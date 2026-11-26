"""Finnur kvikmyndir og sjónvarpsþætti sem eru aðgengileg á RÚV, flettir þeim upp í
gagnasöfnum IMDb og raðar þeim eftir einkunn.

Kvikmyndir koma af flokkasíðunni /sjonvarp/flokkur/kvikmyndir. Þættir hafa enga slíka
síðu, svo þeir eru sóttir úr völdum renningum (fyrirsögnum) á yfirlitssíðunni /sjonvarp/.

IMDb lokar á sjálfvirka skröpun á leitarsíðunni sinni (CAPTCHA), svo í stað
þess eru notuð opinber gagnasöfn IMDb (https://datasets.imdbws.com/, leyfð til
einkanota). Þau eru sótt einu sinni og endurnýjuð vikulega.
"""
import csv
import json
import os
import random
import re
import time
import unicodedata
from pathlib import Path
from urllib.parse import urljoin

import pandas as pd
import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

START_URL = "https://www.ruv.is/sjonvarp/flokkur/kvikmyndir"
YFIRLIT_URL = "https://www.ruv.is/sjonvarp/"
BASE_URL = "https://www.ruv.is"

# Renningar á yfirlitssíðunni sem þættir eru sóttir úr. RÚV breytir þeim stundum;
# fyrirsagnir sem finnast ekki eru einfaldlega hunsaðar.
RENNINGAR = [
    "Leiknar þáttaraðir", "Norrænt", "Breskar þáttaraðir", "Íslenskar þáttaraðir",
    "Heimildarmyndir og fróðleikur", "Íslenskar heimildarmyndir", "Íslenskt heimildarefni",
    "Sakamál og spenna", "Heilsa og líðan", "Hönnun, listir og lífið", "Náttúra og lífríki",
    "Flakk, bras og basl", "Leikið efni fyrir ungt fólk", "Síðasti séns",
]
ISLENSKIR_RENNINGAR = {"Íslenskar þáttaraðir", "Íslenskar heimildarmyndir", "Íslenskt heimildarefni"}
IMDB_GAGNASLOD = "https://datasets.imdbws.com/"

MAPPA = Path(__file__).resolve().parent
GAGNAMAPPA = MAPPA / "imdb_gögn"
UTTAKSSKRA = MAPPA / "rúv_kvikmyndir.csv"
UTTAKSSKRA_THAETTIR = MAPPA / "rúv_þættir.csv"
VEFGOGN = MAPPA / "docs" / "gogn.js"  # lesið af docs/index.html (GitHub Pages)

HAMARKSALDUR_GAGNA = 7 * 24 * 3600  # sekúndur; sækja IMDb-gögn aftur eftir viku
LAGMARKSATKVAEDI = 1000  # m í veginni einkunn: (v·R + m·C) / (v + m)
LAGMARKSATKVAEDI_THAETTIR = 300  # þættir (ekki síst norrænir og íslenskir) fá færri atkvæði
TEGUNDIR = {"movie", "tvMovie", "short", "video", "tvSpecial"}
TEGUNDIR_RADIR = {"tvSeries", "tvMiniSeries"}

# OMDb (https://www.omdbapi.com/) gefur Rotten Tomatoes- og Metacritic-einkunnir eftir IMDb-auðkenni.
# Lykill í umhverfisbreytunni OMDB_API_KEY eða í omdb_lykill.txt (í .gitignore, aldrei í git).
OMDB_SLOD = "https://www.omdbapi.com/"
OMDB_LYKILSKRA = MAPPA / "omdb_lykill.txt"
OMDB_SKYNDIMINNI = GAGNAMAPPA / "omdb.json"
OMDB_HAMARKSALDUR = 30 * 24 * 3600  # sekúndur; einkunnir breytast hægt

session = requests.Session()
session.headers["User-Agent"] = "Mozilla/5.0 (ruv_kvikmyndir; einkaverkefni)"


def saekja(url, **kwargs):
    """GET með tímamörkum og endurtilraunum."""
    for tilraun in range(3):
        try:
            svar = session.get(url, timeout=30, **kwargs)
            svar.raise_for_status()
            return svar
        except requests.RequestException:
            if tilraun == 2:
                raise
            time.sleep(2 ** tilraun)


# ---------------------------------------------------------------- RÚV


def ruv_listi():
    """Skilar {dagskrárliður_id: {...}} fyrir allar kvikmyndir á flokkasíðunni."""
    soup = BeautifulSoup(saekja(START_URL).content, "html.parser")
    myndir = {}
    for a in soup.find_all("a", href=re.compile(r"/sjonvarp/spila/")):
        m = re.search(r"/sjonvarp/spila/[^/]+/(\d+)/(\w+)", a["href"])
        if not m or m.group(1) in myndir:
            continue
        myndir[m.group(1)] = {
            "íslenskur_titill": a.get("aria-label", "").strip(),
            "hlekkur": urljoin(BASE_URL, a["href"]),
            "þáttur_id": m.group(2),
        }
    return myndir


def ruv_renningar(fyrirsagnir):
    """Skilar {dagskrárliður_id: {..., "renningar": [...]}} fyrir alla dagskrárliði í
    renningunum `fyrirsagnir` á yfirlitssíðunni."""
    soup = BeautifulSoup(saekja(YFIRLIT_URL).content, "html.parser")
    slugs = {}
    for section in soup.find_all("section", attrs={"data-slug": True}):
        h2 = section.find("h2")
        if h2:
            slugs[h2.get_text(strip=True)] = section["data-slug"]
    vantar = [f for f in fyrirsagnir if f not in slugs]
    if vantar:
        print("Renningar sem fundust ekki á RÚV (sleppt):", ", ".join(vantar))

    lidir = {}
    for fyrirsogn in fyrirsagnir:
        if fyrirsogn not in slugs:
            continue
        gogn = apollo_gogn(saekja(f"{BASE_URL}/sjonvarp/renningur/{slugs[fyrirsogn]}").text)
        featured = next((v for k, v in gogn.get("ROOT_QUERY", {}).items()
                         if k.startswith("Featured(")), {})
        panel = next((p for k, v in featured.items() if k.startswith("panels(")
                      for p in v or []), {})
        for tilvisun in panel.get("programs") or []:
            dl = gogn.get(tilvisun["__ref"], {})
            thaettir = [e["__ref"].split(":", 1)[1] for e in dl.get("episodes") or []]
            if not dl.get("programID") or not thaettir:
                continue
            id_ = str(dl["programID"])
            lidur = lidir.setdefault(id_, {
                "íslenskur_titill": (dl.get("title") or "").strip(),
                "hlekkur": f"{BASE_URL}/sjonvarp/spila/{dl['slug']}/{id_}/{thaettir[0]}",
                "þáttur_id": thaettir[0],
                "renningar": [],
            })
            lidur["renningar"].append(fyrirsogn)
        time.sleep(random.uniform(0.3, 0.8))
    return lidir


def apollo_gogn(html):
    """RÚV-síður innihalda gögnin sín sem JSON í window.__APOLLO_STATE__."""
    m = re.search(r"window\.__APOLLO_STATE__\s*=\s*(\{.*?\});?\s*</script>",
                  html, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}


def finna_ar(lysing):
    m = re.search(r"frá\s+(?:árinu\s+)?(\d{4})\b", lysing)
    return int(m.group(1)) if m else None


def ruv_upplysingar(id_, mynd):
    """Bætir erlendum titli, lýsingu, ári, lengd og gildistíma við `mynd`."""
    html = saekja(mynd["hlekkur"]).text
    gogn = apollo_gogn(html)
    dagskrarlidur = gogn.get(f"Program:{id_}", {})
    thattur = gogn.get(f"Episode:{mynd['þáttur_id']}", {})

    if dagskrarlidur:
        mynd["íslenskur_titill"] = dagskrarlidur.get("title") or mynd["íslenskur_titill"]
        erl_titill = dagskrarlidur.get("foreign_title")
        lysing = dagskrarlidur.get("description") or ""
        mynd["mynd"] = dagskrarlidur.get("image")
    else:  # varaleið ef RÚV breytir síðunni
        soup = BeautifulSoup(html, "html.parser")
        h3 = soup.find("h3")
        erl_titill = h3.get_text(strip=True) if h3 else None
        malsgreinar = [p.get_text(strip=True) for p in soup.find_all("p")]
        lysing = next((p for p in malsgreinar if p), "")

    mynd["erl_titill"] = (erl_titill or "").strip() or None
    # RÚV setur stundum heiti myndaraðar (t.d. „Íslenskt bíóhaust“) í reitinn fyrir erlendan titil
    if mynd["erl_titill"] and re.match(r"íslenskt bíóhaust", mynd["erl_titill"], re.IGNORECASE):
        mynd["erl_titill"] = None
    mynd["lýsing"] = lysing.strip()
    mynd["ár"] = finna_ar(lysing)
    if thattur.get("duration"):
        mynd["lengd_mín"] = round(thattur["duration"] / 60)
    mynd["aðgengileg_til"] = thattur.get("file_expires")

    # Þættir: fjöldi þátta í spilaranum og fyrsti gildistími (aðeins sumir þættir hafa hann)
    tilvisanir = next((v for k, v in dagskrarlidur.items()
                       if k.startswith("episodes(") and '"limit":50' in k), [])
    thaettir = [gogn[r["__ref"]] for r in tilvisanir if r.get("__ref") in gogn]
    mynd["fjöldi_þátta"] = len(thaettir) or None
    gildistimar = sorted(e["file_expires"] for e in thaettir if e.get("file_expires"))
    if gildistimar:
        mynd["aðgengileg_til"] = gildistimar[0]


# ---------------------------------------------------------------- IMDb


def stadla(titill):
    """Staðlar titil til samanburðar: lágstafir, án kommu/broddstafa og greinis."""
    t = unicodedata.normalize("NFKD", str(titill).casefold().replace("&", " and "))
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = " ".join(re.sub(r"[^\w\s]", " ", t).split())
    return re.sub(r"^(the|a|an) ", "", t)


def saekja_gagnaskra(nafn):
    slod = GAGNAMAPPA / nafn
    if slod.exists() and time.time() - slod.stat().st_mtime < HAMARKSALDUR_GAGNA:
        return slod
    GAGNAMAPPA.mkdir(exist_ok=True)
    hluti = slod.with_name(slod.name + ".part")
    with saekja(IMDB_GAGNASLOD + nafn, stream=True) as svar, open(hluti, "wb") as f:
        heild = int(svar.headers.get("Content-Length", 0)) or None
        with tqdm(total=heild, unit="B", unit_scale=True, desc=f"Sæki {nafn}") as stika:
            for bitar in svar.iter_content(1 << 20):
                f.write(bitar)
                stika.update(len(bitar))
    hluti.replace(slod)
    return slod


def imdb_gogn():
    """Skilar töflu yfir kvikmyndir og þáttaraðir á IMDb sem hafa einkunn, með stöðluðum titlum."""
    grunnur = saekja_gagnaskra("title.basics.tsv.gz")
    einkunnir = saekja_gagnaskra("title.ratings.tsv.gz")
    unnid = GAGNAMAPPA / "imdb_kvikmyndir.pkl"
    if unnid.exists() and unnid.stat().st_mtime >= max(
            grunnur.stat().st_mtime, einkunnir.stat().st_mtime):
        myndir = pd.read_pickle(unnid)
        if "endYear" in myndir:  # annars eldri útgáfa; vinna aftur
            return myndir

    print("Vinn úr IMDb-gögnum (gert einu sinni eftir hvert niðurhal)...")
    lesa = dict(sep="\t", quoting=csv.QUOTE_NONE, dtype=str,
                na_values=["\\N"], keep_default_na=False)
    bitar = pd.read_csv(grunnur, usecols=["tconst", "titleType", "primaryTitle",
                                          "originalTitle", "startYear", "endYear", "genres"],
                        chunksize=500_000, **lesa)
    myndir = pd.concat(b[b["titleType"].isin(TEGUNDIR | TEGUNDIR_RADIR)] for b in bitar)
    myndir = myndir.merge(pd.read_csv(einkunnir, **lesa), on="tconst")
    myndir["startYear"] = pd.to_numeric(myndir["startYear"], errors="coerce")
    myndir["endYear"] = pd.to_numeric(myndir["endYear"], errors="coerce")
    myndir["averageRating"] = myndir["averageRating"].astype(float)
    myndir["numVotes"] = myndir["numVotes"].astype(int)
    myndir["n_aðal"] = myndir["primaryTitle"].map(stadla)
    myndir["n_upprunal"] = myndir["originalTitle"].map(stadla)
    myndir = myndir.reset_index(drop=True)
    myndir.to_pickle(unnid)
    return myndir


def an_radar(titill):
    """„Laundóttirin II“ → „Laundóttirin“, „Dalgliesh III“ → „Dalgliesh“, „Úti 3“ → „Úti“:
    IMDb skráir þáttaröð í heild, ekki hverja seríu."""
    if not titill:
        return None
    styttur = re.sub(r"[\s:–-]+(?:(?:season|series|sería|þáttaröð)\s*\d{1,2}|[IVX]+|\d{1,2})$",
                     "", titill, flags=re.IGNORECASE).strip()
    return styttur if styttur and styttur != titill else None


def imdb_uppfletting(imdb, titlar, ar, tegundir=TEGUNDIR, radir_fyrst=False,
                     strangt=False, upprunalegir=()):
    """Finnur bestu samsvörun: sami (staðlaði) titill og ár ±1.

    Þáttaraðir mega hafa byrjað fyrr: árið í lýsingu RÚV á oft við seríuna sem er sýnd,
    svo það má vera hvar sem er á tímabili raðarinnar. Sé árið óþekkt er tekinn sá titill
    sem flest atkvæði hefur. `radir_fyrst` tekur þáttaraðir fram yfir stakar myndir.

    `strangt`: sé árið óþekkt og fleiri en einn titill kemur til greina er aðeins tekinn
    sá vinsælasti ef hann hefur a.m.k. tífalt fleiri atkvæði en sá næsti (annars gæti
    norskur þáttur sem heitir „Gift“ fengið einkunn Hollywood-myndarinnar „The Gift“).
    Titlar í `upprunalegir` (íslenskir titlar) verða að passa við upprunalegan titil á IMDb.
    """
    imdb = imdb[imdb["titleType"].isin(tegundir)]
    for titill in titlar:
        q = stadla(titill) if titill else ""
        if not q:
            continue
        if titill in upprunalegir:
            frambjodendur = imdb[imdb["n_upprunal"] == q]
        else:
            frambjodendur = imdb[(imdb["n_aðal"] == q) | (imdb["n_upprunal"] == q)]
        er_rod = frambjodendur["titleType"].isin(TEGUNDIR_RADIR)
        if ar:
            lok = frambjodendur["endYear"].fillna(time.localtime().tm_year)
            innan = (frambjodendur["startYear"] - ar).abs() <= 1
            innan |= er_rod & (frambjodendur["startYear"] - 1 <= ar) & (ar <= lok + 1)
            frambjodendur, er_rod = frambjodendur[innan], er_rod[innan]
        if frambjodendur.empty:
            continue
        rett_ar = frambjodendur["startYear"] == ar if ar else False
        rod = er_rod if radir_fyrst else ~er_rod
        rodud = (frambjodendur.assign(rett_ar=rett_ar, rod=rod)
                 .sort_values(["rod", "rett_ar", "numVotes"], ascending=False))
        if strangt and not ar:
            # Aðeins borið saman innan þeirrar tegundar sem er tekin fram yfir hina
            rodud = rodud[rodud["rod"] == rodud["rod"].iloc[0]]
            # Nákvæmlega sami titill (t.d. „Kanarí“, ekki „Kanari“ eða „The Gift“) gengur fyrir
            nakvaem = (rodud["primaryTitle"].str.casefold() == titill.casefold()) | (
                rodud["originalTitle"].str.casefold() == titill.casefold())
            if nakvaem.any():
                rodud = rodud[nakvaem]
            atkv = rodud["numVotes"].tolist()
            if len(atkv) > 1 and atkv[0] < 5 * atkv[1]:
                continue
        return rodud.iloc[0]
    return None


def lysing_segir_rod(lysing):
    """True/False ef lýsing RÚV segir efnið vera þáttaröð/mynd (það orð sem kemur fyrst
    ræður: „Heimildarmynd um … þáttaröðinni X“ er mynd), None ef hvorugt kemur fram."""
    m = re.search(r"(þáttar[öa]ð|\bþátt(?:um|unum)\b|\bþættir\b)|(mynd)", lysing or "", re.IGNORECASE)
    return None if not m else bool(m.group(1))


def er_islensk(mynd, nidurstada):
    """Íslensk ef fyrsta setning lýsingar RÚV segir það („Íslensk kvikmynd frá…“), ef efnið
    er í íslenskum renningi, eða ef það hefur engan erlendan titil og upprunalegi titillinn
    á IMDb er sá íslenski."""
    if ISLENSKIR_RENNINGAR & set(mynd.get("renningar") or []):
        return True
    fyrsta_setning = re.split(r"(?<=[.!?])\s", mynd.get("lýsing") or "", maxsplit=1)[0]
    if re.search(r"\bíslensk", fyrsta_setning, re.IGNORECASE):
        return True
    if nidurstada is not None and not mynd.get("erl_titill"):
        return stadla(nidurstada["originalTitle"]) == stadla(mynd["íslenskur_titill"])
    return False


# ---------------------------------------------------------------- OMDb


def omdb_lykill():
    lykill = os.environ.get("OMDB_API_KEY", "").strip()
    if not lykill and OMDB_LYKILSKRA.exists():
        lykill = OMDB_LYKILSKRA.read_text(encoding="utf-8").strip()
    return lykill or None


def lesa_omdb_einkunnir(svar):
    """Breytir Ratings-lista OMDb í tölur á kvarðanum 0–100.

    „96%“ (Rotten Tomatoes) → 96, „86/100“ (Metacritic) → 86.
    """
    einkunnir = {}
    heiti = {"Rotten Tomatoes": "rt", "Metacritic": "mc"}
    for e in svar.get("Ratings") or []:
        lykill = heiti.get(e.get("Source"))
        m = re.match(r"\s*(\d+(?:\.\d+)?)\s*(%|/\s*100)", e.get("Value") or "")
        if lykill and m:
            einkunnir[lykill] = round(float(m.group(1)))
    # Upprunaland, notað til að staðfesta samsvaranir þátta sem hafa ekkert ár
    einkunnir["land"] = svar.get("Country") if svar.get("Country") not in (None, "N/A") else None
    return einkunnir


def omdb_einkunnir(tconst_listi):
    """Skilar {tconst: {"rt": .., "mc": ..}} og geymir svör í skyndiminni.

    Án lykils er aðeins notað það sem þegar er í skyndiminni.
    """
    try:
        minni = json.loads(OMDB_SKYNDIMINNI.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        minni = {}
    lykill = omdb_lykill()
    vantar = [t for t in tconst_listi  # eldri færslur án „land“ eru sóttar aftur
              if time.time() - minni.get(t, {}).get("sótt", 0) > OMDB_HAMARKSALDUR
              or "land" not in minni[t]]
    if vantar and not lykill:
        print(f"Enginn OMDb-lykill (OMDB_API_KEY eða {OMDB_LYKILSKRA.name}); "
              f"sleppi Rotten Tomatoes/Metacritic fyrir {len(vantar)} myndir.")
        vantar = []

    for tconst in tqdm(vantar, desc="Sæki einkunnir frá OMDb"):
        try:
            svar = session.get(OMDB_SLOD, params={"i": tconst, "apikey": lykill},
                               timeout=30).json()
        except (requests.RequestException, ValueError) as villa:
            tqdm.write(f"OMDb-villa við {tconst}: {villa}")
            continue
        if svar.get("Response") == "False" and "not found" not in svar.get("Error", "").lower():
            # Rangur lykill eða dagskvóti búinn; reyna aftur í næstu keyrslu
            tqdm.write(f"OMDb: {svar.get('Error')}. Hætti að sækja.")
            break
        minni[tconst] = {"sótt": int(time.time()), **lesa_omdb_einkunnir(svar)}
        time.sleep(0.1)

    if vantar:
        GAGNAMAPPA.mkdir(exist_ok=True)
        OMDB_SKYNDIMINNI.write_text(json.dumps(minni, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
    return {t: minni[t] for t in tconst_listi if t in minni}


# ---------------------------------------------------------------- Keyrsla


def skrifa_vefgogn(myndir, thaettir):
    """Skrifar gögnin sem JS-skrá svo docs/index.html virki bæði á GitHub Pages
    og þegar hún er opnuð beint af diski (fetch() virkar ekki á file://)."""
    gogn = {
        "uppfært": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "myndir": json.loads(myndir.to_json(orient="records", force_ascii=False)),
        "þættir": json.loads(thaettir.to_json(orient="records", force_ascii=False)),
    }
    VEFGOGN.parent.mkdir(exist_ok=True)
    VEFGOGN.write_text("window.RUV_GOGN = " + json.dumps(gogn, ensure_ascii=False, indent=1)
                       + ";\n", encoding="utf-8")


def saekja_ruv_upplysingar(lidir, lysing):
    for id_, lidur in tqdm(lidir.items(), desc=lysing):
        try:
            ruv_upplysingar(id_, lidur)
        except Exception as villa:  # ein biluð síða á ekki að stöðva keyrsluna
            tqdm.write(f"Villa við {lidur['hlekkur']}: {villa}")
        time.sleep(random.uniform(0.5, 1.5))


def tengja_imdb(imdb, lidir, thaettir=False):
    for lidur in lidir.values():
        titlar = [lidur.get("erl_titill"), lidur["íslenskur_titill"]]
        if thaettir:
            titlar += [an_radar(t) for t in titlar]
            # Þáttaröð að sögn RÚV → aðeins þáttaraðir á IMDb. Margir þættir í spilaranum
            # → þáttaraðir ganga fyrir (en RÚV skiptir stundum myndum í hluta).
            texti = lysing_segir_rod(lidur.get("lýsing"))
            nidurstada = imdb_uppfletting(
                imdb, titlar, lidur.get("ár"),
                tegundir=TEGUNDIR_RADIR if texti else TEGUNDIR | TEGUNDIR_RADIR,
                radir_fyrst=bool(texti) or (lidur.get("fjöldi_þátta") or 0) > 1,
                strangt=True,
                upprunalegir={lidur["íslenskur_titill"], an_radar(lidur["íslenskur_titill"])})
        else:
            nidurstada = imdb_uppfletting(imdb, titlar, lidur.get("ár"))
        lidur["íslensk"] = er_islensk(lidur, nidurstada)
        if nidurstada is None:
            continue
        lidur["imdb_einkunn"] = nidurstada["averageRating"]
        lidur["imdb_atkvæði"] = nidurstada["numVotes"]
        lidur["imdb_titill"] = nidurstada["primaryTitle"]
        lidur["imdb_tegund"] = nidurstada["titleType"]
        if pd.notna(nidurstada["genres"]):
            lidur["imdb_flokkar"] = nidurstada["genres"]
        if pd.notna(nidurstada["startYear"]):
            lidur["imdb_ár"] = int(nidurstada["startYear"])
        lidur["imdb_hlekkur"] = f"https://www.imdb.com/title/{nidurstada['tconst']}/"
        lidur["tconst"] = nidurstada["tconst"]


# Þjóðerni í lýsingum RÚV („norsk þáttaröð“, „í þessum dönsku þáttum“) → land eins og OMDb ritar það
THJODERNI = {
    "norsk": "Norway", "sænsk": "Sweden", "dönsk": "Denmark", "dansk": "Denmark",
    "finnsk": "Finland", "færeysk": "Faroe Islands", "íslensk": "Iceland",
    "grænlensk": "Greenland", "bresk": "United Kingdom", "bandarísk": "United States",
    "kanadísk": "Canada", "áströlsk": "Australia", "ástralsk": "Australia", "þýsk": "Germany",
    "frönsk": "France", "fransk": "France", "ítölsk": "Italy", "ítalsk": "Italy",
    "spænsk": "Spain", "pólsk": "Poland", "hollensk": "Netherlands", "írsk": "Ireland",
    "belgísk": "Belgium", "portúgölsk": "Portugal", "portúgalsk": "Portugal",
    "brasilísk": "Brazil", "japönsk": "Japan", "japansk": "Japan",
}


def land_stangast_a(lysing, land):
    """True ef lýsing RÚV nefnir þjóðerni en ekkert þeirra er meðal upprunalanda á OMDb."""
    if not land:
        return False
    nefnd = {THJODERNI[m.casefold()] for m in re.findall(
        r"\b(" + "|".join(THJODERNI) + r")", lysing or "", re.IGNORECASE)}
    return bool(nefnd) and not any(n in land for n in nefnd)


def tafla(lidir, dalkar, lagmark):
    df = pd.DataFrame(list(lidir.values()))
    for dalkur in ("imdb_einkunn", "imdb_atkvæði", "rt_einkunn", "mc_einkunn"):
        if dalkur not in df:
            df[dalkur] = float("nan")
    # Vegin einkunn (eins og IMDb Top 250) svo efni með fá atkvæði trónir ekki efst
    medaltal = df["imdb_einkunn"].mean()
    v, r, m = df["imdb_atkvæði"], df["imdb_einkunn"], lagmark
    df["vegin_einkunn"] = ((v * r + m * medaltal) / (v + m)).round(2)
    # Rotten Tomatoes og Metacritic eru aðeins sýndar, ekki notaðar í röðun: fæstar íslenskar
    # myndir hafa þær, og RT (hlutfall jákvæðra dóma) myndi lyfta þeim myndum sem hafa þær.
    for dalkur in ("rt_einkunn", "mc_einkunn"):
        df[dalkur] = pd.to_numeric(df[dalkur], errors="coerce")
    heiltolur = {d: "Int64" for d in ("ár", "imdb_ár", "imdb_atkvæði", "rt_einkunn",
                                      "mc_einkunn", "lengd_mín", "fjöldi_þátta") if d in dalkar}
    return (df.reindex(columns=dalkar)
            .sort_values(["vegin_einkunn", "imdb_einkunn"], ascending=False, na_position="last")
            .astype(heiltolur))


def prenta_samantekt(df, heiti, skra):
    fundnar = df["imdb_einkunn"].notna()
    print(f"\n{fundnar.sum()} af {len(df)} {heiti} fundust á IMDb. Vistað í {skra.name}\n")
    print(df.loc[fundnar, ["íslenskur_titill", "ár", "vegin_einkunn", "imdb_einkunn",
                           "rt_einkunn", "mc_einkunn", "aðgengileg_til"]].head(15).to_string(index=False))
    if (~fundnar).any():
        print("\nFundust ekki á IMDb:")
        for _, rod in df[~fundnar].iterrows():
            erl = rod["erl_titill"] if pd.notna(rod["erl_titill"]) else "–"
            ar = rod["ár"] if pd.notna(rod["ár"]) else "?"
            print(f"  {rod['íslenskur_titill']} ({erl}, {ar})")


def main():
    myndir = ruv_listi()
    if not myndir:
        raise SystemExit(f"Engar kvikmyndir fundust á {START_URL}; "
                         "hefur RÚV breytt síðunni?")
    print("Alls eru", len(myndir), "kvikmyndir í boði á RÚV")
    # Kvikmyndir sem eru líka í renningi (t.d. „Síðasti séns“) eru aðeins taldar sem kvikmyndir
    thaettir = {k: v for k, v in ruv_renningar(RENNINGAR).items() if k not in myndir}
    print("og", len(thaettir), "aðrir dagskrárliðir í völdum renningum")

    saekja_ruv_upplysingar(myndir, "Sæki upplýsingar um kvikmyndir frá RÚV")
    saekja_ruv_upplysingar(thaettir, "Sæki upplýsingar um þætti frá RÚV")

    imdb = imdb_gogn()
    tengja_imdb(imdb, myndir)
    tengja_imdb(imdb, thaettir, thaettir=True)

    allt = list(myndir.values()) + list(thaettir.values())
    omdb = omdb_einkunnir(list(dict.fromkeys(m["tconst"] for m in allt if "tconst" in m)))
    # Þættir án árs geta lent á röngum titli með sama nafni (t.d. japönskum „Gift“ í stað
    # norsks); hafna samsvörun ef upprunaland á OMDb passar ekki við lýsingu RÚV
    for lidur in thaettir.values():
        e = omdb.get(lidur.get("tconst"), {})
        if not lidur.get("ár") and land_stangast_a(lidur.get("lýsing"), e.get("land")):
            print(f"Hafna IMDb-samsvörun {lidur['íslenskur_titill']} → {lidur['imdb_titill']} "
                  f"({e['land']})")
            for k in [k for k in lidur if k.startswith("imdb_") or k == "tconst"]:
                del lidur[k]
    for lidur in allt:
        e = omdb.get(lidur.get("tconst"), {})
        lidur["rt_einkunn"] = e.get("rt")
        lidur["mc_einkunn"] = e.get("mc")

    dalkar = ["íslenskur_titill", "erl_titill", "ár", "íslensk", "vegin_einkunn", "imdb_einkunn",
              "imdb_atkvæði", "rt_einkunn", "mc_einkunn", "lengd_mín", "aðgengileg_til",
              "imdb_titill", "imdb_ár", "imdb_hlekkur", "imdb_flokkar", "lýsing", "hlekkur", "mynd"]
    df_myndir = tafla(myndir, dalkar, LAGMARKSATKVAEDI)
    dalkar_th = (dalkar[:10] + ["fjöldi_þátta"] + dalkar[10:13] + ["imdb_tegund"]
                 + dalkar[13:] + ["renningar"])
    df_thaettir = tafla(thaettir, dalkar_th, LAGMARKSATKVAEDI_THAETTIR)

    # utf-8-sig svo Excel sýni íslenska stafi rétt
    df_myndir.to_csv(UTTAKSSKRA, index=False, encoding="utf-8-sig")
    (df_thaettir.assign(renningar=df_thaettir["renningar"].map(" | ".join))
     .to_csv(UTTAKSSKRA_THAETTIR, index=False, encoding="utf-8-sig"))
    skrifa_vefgogn(df_myndir, df_thaettir)

    prenta_samantekt(df_myndir, "myndum", UTTAKSSKRA)
    prenta_samantekt(df_thaettir, "þáttum", UTTAKSSKRA_THAETTIR)


if __name__ == "__main__":
    main()
