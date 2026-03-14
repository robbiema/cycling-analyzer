import os
import re
import base64
import json
import logging
import anthropic
import requests
import pandas as pd
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify, render_template

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "nl-BE,nl;q=0.9,en;q=0.8",
    "Referer": "https://firstcycling.com/",
})


# ── FirstCycling scraping (modelled on baronet2/FirstCyclingAPI) ──────────────

def fc_search_rider(name: str) -> int | None:
    """Search FC by name, return rider_id. Mirrors Rider.search()."""
    try:
        resp = SESSION.get(f"https://firstcycling.com/search.php", params={"q": name, "cat": "rider"}, timeout=10, allow_redirects=True)
        # Direct redirect to rider page
        if "rider.php?r=" in resp.url:
            m = re.search(r"r=(\d+)", resp.url)
            if m:
                return int(m.group(1))
        soup = BeautifulSoup(resp.text, "lxml")
        link = soup.find("a", href=re.compile(r"/rider\.php\?r=\d+"))
        if link:
            m = re.search(r"r=(\d+)", link["href"])
            if m:
                return int(m.group(1))
    except Exception as e:
        log.warning(f"fc_search_rider({name}): {e}")
    return None


def fc_year_results(rider_id: int, year: int) -> pd.DataFrame:
    """
    Fetch rider results for a year. Returns DataFrame with columns:
    Date, Pos, Race, CAT  — exactly like Rider(id).year_results(year).results_df
    """
    try:
        resp = SESSION.get(f"https://firstcycling.com/rider.php", params={"r": rider_id, "y": year}, timeout=10)
        if resp.status_code != 200:
            return pd.DataFrame()
        soup = BeautifulSoup(resp.text, "lxml")

        # Find the results table — has thead with "Pos" header
        for table in soup.find_all("table"):
            thead = table.find("thead")
            if not thead:
                continue
            ths = [th.get_text(strip=True) for th in thead.find_all("th")]
            if "Pos" not in ths:
                continue

            # Found it — map column indices
            pos_i = ths.index("Pos")
            # Race is usually the column after the flag (image) column
            # Typical FC layout: Date | Pos | [flag] | Race | CAT | UCI pts | ...
            # Find Race col: first text column after pos that isn't a number-like header
            race_i = None
            cat_i = None
            for i, h in enumerate(ths):
                if i <= pos_i:
                    continue
                if h in ("Race", "Wedstrijd", ""):
                    if race_i is None:
                        race_i = i
                elif h in ("CAT", "Cat", "Category"):
                    cat_i = i

            # Fallback: race is pos+2 (after flag col), cat is pos+3
            if race_i is None:
                race_i = pos_i + 2
            if cat_i is None:
                cat_i = pos_i + 3

            rows = []
            for tr in table.find_all("tr"):
                tds = tr.find_all("td")
                if not tds:
                    continue
                # Validate: first cell must look like a date DD.MM
                date_text = tds[0].get_text(strip=True)
                if not re.match(r'^\d{1,2}\.\d{1,2}$', date_text):
                    continue
                if len(tds) <= max(pos_i, race_i):
                    continue

                pos_text = re.sub(r'[^\d]', '', tds[pos_i].get_text(strip=True))
                if not pos_text or len(pos_text) > 3:
                    continue
                pos = int(pos_text)
                if pos == 0 or pos > 300:
                    continue

                race = tds[race_i].get_text(strip=True) if race_i < len(tds) else ""
                cat = tds[cat_i].get_text(strip=True) if cat_i < len(tds) else ""

                if not race or len(race) < 3:
                    continue

                rows.append({"Date": date_text, "Pos": pos, "Race": race, "CAT": cat})

            return pd.DataFrame(rows)

    except Exception as e:
        log.warning(f"fc_year_results({rider_id}, {year}): {e}")
    return pd.DataFrame()


def get_rider_stats(rider_id: int) -> dict:
    """Aggregate wins, podiums, top10s across 2023-2025."""
    wins, podiums, top10s, notable = 0, 0, 0, []
    for year in (2023, 2024, 2025):
        df = fc_year_results(rider_id, year)
        if df.empty:
            continue
        for _, row in df.iterrows():
            pos = row["Pos"]
            race = row["Race"]
            if pos <= 10:
                top10s += 1
                if pos <= 3:
                    podiums += 1
                    if pos == 1:
                        wins += 1
                        notable.append(f"🏆 {race} ({year})")
                    else:
                        notable.append(f"P{pos} {race} ({year})")
    return {"wins": wins, "podiums": podiums, "top10s": top10s, "notable": notable[:4]}


# ── Claude image extraction ───────────────────────────────────────────────────

def extract_riders_from_image(image_b64: str, mime_type: str) -> list[dict]:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4000,
        system="""You read Belgian cycling race start lists. Extract every rider.
Format: NUMBER  SURNAME Firstname  TEAM  Category  UCI_ID  UCIcld
Return ONLY valid JSON:
{"riders":[{"bib":"55","name":"Firstname Surname","team":"Team Name"}]}
Convert ALL-CAPS names: VAN DEN BERGHE Nathan -> Nathan Van Den Berghe""",
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": image_b64}},
                {"type": "text", "text": "Extract all riders. Return only JSON."}
            ]
        }]
    )
    text = response.content[0].text.strip()
    text = re.sub(r'^```json\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    return json.loads(text).get("riders", [])


# ── Worker ────────────────────────────────────────────────────────────────────

def process_rider(rider: dict) -> dict:
    result = {**rider, "wins": None, "podiums": None, "top10s": None, "notable": [], "found": False}
    name = rider["name"]

    rider_id = fc_search_rider(name)
    # If not found, try reversed (Lastname Firstname)
    if not rider_id:
        parts = name.split()
        if len(parts) >= 2:
            reversed_name = f"{parts[-1]} {' '.join(parts[:-1])}"
            rider_id = fc_search_rider(reversed_name)

    if not rider_id:
        log.info(f"✗ {name}")
        return result

    log.info(f"✓ {name} -> {rider_id}")
    stats = get_rider_stats(rider_id)
    log.info(f"  W={stats['wins']} P={stats['podiums']} T10={stats['top10s']}")
    result.update({**stats, "found": True})
    return result


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/analyze", methods=["POST"])
def analyze():
    files = request.files.getlist("images")
    if not files:
        return jsonify({"error": "No images uploaded"}), 400

    all_riders = []
    for f in files[:2]:
        raw = f.read()
        b64 = base64.b64encode(raw).decode()
        mime = f.content_type or "image/jpeg"
        if len(raw) > 3_000_000:
            try:
                from PIL import Image
                import io
                img = Image.open(io.BytesIO(raw))
                img.thumbnail((1400, 1400))
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=85)
                b64 = base64.b64encode(buf.getvalue()).decode()
                mime = "image/jpeg"
            except Exception:
                pass
        try:
            all_riders.extend(extract_riders_from_image(b64, mime))
        except Exception as e:
            return jsonify({"error": f"Failed to extract riders: {e}"}), 500

    # Deduplicate
    seen, unique = set(), []
    for r in all_riders:
        if r["name"] not in seen:
            seen.add(r["name"])
            unique.append(r)

    if not unique:
        return jsonify({"error": "No riders found in images"}), 400

    log.info(f"Processing {len(unique)} riders...")

    enriched = []
    with ThreadPoolExecutor(max_workers=12) as ex:
        futures = {ex.submit(process_rider, r): r for r in unique}
        for f in as_completed(futures):
            try:
                enriched.append(f.result())
            except Exception as e:
                log.warning(f"Worker error: {e}")
                enriched.append(futures[f])

    found = sorted([r for r in enriched if r.get("found")],
                   key=lambda r: (-(r.get("podiums") or 0), -(r.get("wins") or 0), -(r.get("top10s") or 0)))
    not_found = [r for r in enriched if not r.get("found")]

    return jsonify({
        "total_riders": len(unique),
        "found_on_fc": len(found),
        "rankings": (found + not_found)[:10]
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
