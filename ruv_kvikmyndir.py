"""Finnur kvikmyndir sem eru aðgengilegar á RÚV, flettir þeim upp í
gagnasöfnum IMDb og raðar þeim eftir einkunn.

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
BASE_URL = "https://www.ruv.is"
IMDB_GAGNASLOD = "https://datasets.imdbws.com/"

MAPPA = Path(__file__).resolve().parent
GAGNAMAPPA = MAPPA / "imdb_gögn"
UTTAKSSKRA = MAPPA / "rúv_kvikmyndir.csv"
VEFGOGN = MAPPA / "docs" / "gogn.js"  # lesið af docs/index.html (GitHub Pages)

HAMARKSALDUR_GAGNA = 7 * 24 * 3600  # sekúndur; sækja IMDb-gögn aftur eftir viku
LAGMARKSATKVAEDI = 1000  # m í veginni einkunn: (v·R + m·C) / (v + m)
TEGUNDIR = {"movie", "tvMovie", "short", "video", "tvSpecial"}

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
    """Skilar töflu yfir kvikmyndir á IMDb sem hafa einkunn, með stöðluðum titlum."""
    grunnur = saekja_gagnaskra("title.basics.tsv.gz")
    einkunnir = saekja_gagnaskra("title.ratings.tsv.gz")
    unnid = GAGNAMAPPA / "imdb_kvikmyndir.pkl"
    if unnid.exists() and unnid.stat().st_mtime >= max(
            grunnur.stat().st_mtime, einkunnir.stat().st_mtime):
        myndir = pd.read_pickle(unnid)
        if "genres" in myndir:  # annars eldri útgáfa; vinna aftur
            return myndir

    print("Vinn úr IMDb-gögnum (gert einu sinni eftir hvert niðurhal)...")
    lesa = dict(sep="\t", quoting=csv.QUOTE_NONE, dtype=str,
                na_values=["\\N"], keep_default_na=False)
    bitar = pd.read_csv(grunnur, usecols=["tconst", "titleType", "primaryTitle",
                                          "originalTitle", "startYear", "genres"],
                        chunksize=500_000, **lesa)
    myndir = pd.concat(b[b["titleType"].isin(TEGUNDIR)] for b in bitar)
    myndir = myndir.merge(pd.read_csv(einkunnir, **lesa), on="tconst")
    myndir["startYear"] = pd.to_numeric(myndir["startYear"], errors="coerce")
    myndir["averageRating"] = myndir["averageRating"].astype(float)
    myndir["numVotes"] = myndir["numVotes"].astype(int)
    myndir["n_aðal"] = myndir["primaryTitle"].map(stadla)
    myndir["n_upprunal"] = myndir["originalTitle"].map(stadla)
    myndir = myndir.reset_index(drop=True)
    myndir.to_pickle(unnid)
    return myndir


def imdb_uppfletting(imdb, titlar, ar):
    """Finnur bestu samsvörun: sami (staðlaði) titill og ár ±1.

    Sé árið óþekkt er tekin sú mynd með titilinn sem flest atkvæði hefur.
    """
    for titill in titlar:
        q = stadla(titill) if titill else ""
        if not q:
            continue
        frambjodendur = imdb[(imdb["n_aðal"] == q) | (imdb["n_upprunal"] == q)]
        if ar:
            frambjodendur = frambjodendur[(frambjodendur["startYear"] - ar).abs() <= 1]
        if frambjodendur.empty:
            continue
        rett_ar = frambjodendur["startYear"] == ar if ar else False
        return (frambjodendur.assign(rett_ar=rett_ar)
                .sort_values(["rett_ar", "numVotes"], ascending=False)
                .iloc[0])
    return None


def er_islensk(mynd, nidurstada):
    """Íslensk ef fyrsta setning lýsingar RÚV segir það („Íslensk kvikmynd frá…“),
    eða ef myndin hefur engan erlendan titil og upprunalegi titillinn á IMDb er sá íslenski."""
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
    vantar = [t for t in tconst_listi
              if time.time() - minni.get(t, {}).get("sótt", 0) > OMDB_HAMARKSALDUR]
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


def skrifa_vefgogn(df):
    """Skrifar gögnin sem JS-skrá svo docs/index.html virki bæði á GitHub Pages
    og þegar hún er opnuð beint af diski (fetch() virkar ekki á file://)."""
    gogn = {
        "uppfært": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "myndir": json.loads(df.to_json(orient="records", force_ascii=False)),
    }
    VEFGOGN.parent.mkdir(exist_ok=True)
    VEFGOGN.write_text("window.RUV_GOGN = " + json.dumps(gogn, ensure_ascii=False, indent=1)
                       + ";\n", encoding="utf-8")


def main():
    myndir = ruv_listi()
    if not myndir:
        raise SystemExit(f"Engar kvikmyndir fundust á {START_URL}; "
                         "hefur RÚV breytt síðunni?")
    print("Alls eru", len(myndir), "kvikmyndir í boði á RÚV")

    for id_, mynd in tqdm(myndir.items(), desc="Sæki upplýsingar frá RÚV"):
        try:
            ruv_upplysingar(id_, mynd)
        except Exception as villa:  # ein biluð síða á ekki að stöðva keyrsluna
            tqdm.write(f"Villa við {mynd['hlekkur']}: {villa}")
        time.sleep(random.uniform(0.5, 1.5))

    imdb = imdb_gogn()
    for mynd in myndir.values():
        nidurstada = imdb_uppfletting(
            imdb, [mynd.get("erl_titill"), mynd["íslenskur_titill"]], mynd.get("ár"))
        mynd["íslensk"] = er_islensk(mynd, nidurstada)
        if nidurstada is None:
            continue
        mynd["imdb_einkunn"] = nidurstada["averageRating"]
        mynd["imdb_atkvæði"] = nidurstada["numVotes"]
        mynd["imdb_titill"] = nidurstada["primaryTitle"]
        if pd.notna(nidurstada["genres"]):
            mynd["imdb_flokkar"] = nidurstada["genres"]
        if pd.notna(nidurstada["startYear"]):
            mynd["imdb_ár"] = int(nidurstada["startYear"])
        mynd["imdb_hlekkur"] = f"https://www.imdb.com/title/{nidurstada['tconst']}/"
        mynd["tconst"] = nidurstada["tconst"]

    omdb = omdb_einkunnir([m["tconst"] for m in myndir.values() if "tconst" in m])
    for mynd in myndir.values():
        e = omdb.get(mynd.get("tconst"), {})
        mynd["rt_einkunn"] = e.get("rt")
        mynd["mc_einkunn"] = e.get("mc")

    df = pd.DataFrame(list(myndir.values()))
    if "imdb_einkunn" not in df:
        df["imdb_einkunn"] = df["imdb_atkvæði"] = float("nan")
    # Vegin einkunn (eins og IMDb Top 250) svo myndir með fá atkvæði trónir ekki efst
    medaltal = df["imdb_einkunn"].mean()
    v, r, m = df["imdb_atkvæði"], df["imdb_einkunn"], LAGMARKSATKVAEDI
    df["vegin_einkunn"] = ((v * r + m * medaltal) / (v + m)).round(2)
    # Rotten Tomatoes og Metacritic eru aðeins sýndar, ekki notaðar í röðun: fæstar íslenskar
    # myndir hafa þær, og RT (hlutfall jákvæðra dóma) myndi lyfta þeim myndum sem hafa þær.
    for dalkur in ("rt_einkunn", "mc_einkunn"):
        df[dalkur] = pd.to_numeric(df[dalkur], errors="coerce")

    dalkar = ["íslenskur_titill", "erl_titill", "ár", "íslensk", "vegin_einkunn", "imdb_einkunn",
              "imdb_atkvæði", "rt_einkunn", "mc_einkunn", "lengd_mín", "aðgengileg_til",
              "imdb_titill", "imdb_ár", "imdb_hlekkur", "imdb_flokkar", "lýsing", "hlekkur", "mynd"]
    df = (df.reindex(columns=dalkar)
          .sort_values(["vegin_einkunn", "imdb_einkunn"], ascending=False, na_position="last")
          .astype({"ár": "Int64", "imdb_ár": "Int64", "imdb_atkvæði": "Int64",
                   "rt_einkunn": "Int64", "mc_einkunn": "Int64", "lengd_mín": "Int64"}))
    # utf-8-sig svo Excel sýni íslenska stafi rétt
    df.to_csv(UTTAKSSKRA, index=False, encoding="utf-8-sig")
    skrifa_vefgogn(df)

    fundnar = df["imdb_einkunn"].notna()
    print(f"\n{fundnar.sum()} af {len(df)} myndum fundust á IMDb. Vistað í {UTTAKSSKRA.name}\n")
    print(df.loc[fundnar, ["íslenskur_titill", "ár", "vegin_einkunn", "imdb_einkunn",
                           "rt_einkunn", "mc_einkunn", "aðgengileg_til"]].head(15).to_string(index=False))
    if (~fundnar).any():
        print("\nFundust ekki á IMDb:")
        for _, rod in df[~fundnar].iterrows():
            erl = rod["erl_titill"] if pd.notna(rod["erl_titill"]) else "–"
            ar = rod["ár"] if pd.notna(rod["ár"]) else "?"
            print(f"  {rod['íslenskur_titill']} ({erl}, {ar})")


if __name__ == "__main__":
    main()
