import os
import re
import base64
import json
import logging
import anthropic
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify, render_template

# Use the FirstCyclingAPI library for reliable scraping
try:
    from first_cycling_api import Rider
    from first_cycling_api.endpoints import search_rider
    USE_LIBRARY = True
except ImportError:
    USE_LIBRARY = False

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

FC_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "nl-BE,nl;q=0.9,en;q=0.8",
    "Referer": "https://firstcycling.com/",
}


def extract_riders_from_image(image_b64: str, mime_type: str) -> list[dict]:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4000,
        system="""You read Belgian cycling race start lists. Extract every rider.
Format is usually: NUMBER  SURNAME Firstname  TEAM  Category  UCI_ID  UCIcld
UCI_ID looks like BEL20060706 or NOR20050410 (3 letter country code + 8 digits).

Return ONLY valid JSON, nothing else:
{"riders":[{"bib":"55","name":"Firstname Surname","team":"Team Name","uci_id":"BEL20060706"}]}

Name conversion: VAN DEN BERGHE Nathan -> Nathan Van Den Berghe
Extract the UCI_ID carefully from the UCI column.""",
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": image_b64}},
                {"type": "text", "text": "Extract all riders including their UCI ID. Return only JSON."}
            ]
        }]
    )
    text = response.content[0].text.strip()
    text = re.sub(r'^```json\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    data = json.loads(text)
    return data.get("riders", [])


def find_rider_id(name: str) -> int | None:
    """Find rider FC ID. Try library first, fall back to raw search."""

    # Method 1: Use the library's search if available
    if USE_LIBRARY:
        try:
            results = search_rider(name)
            if results and len(results) > 0:
                # Returns list of (id, name) tuples or similar
                first = results[0]
                if isinstance(first, (list, tuple)):
                    return int(first[0])
                elif isinstance(first, dict):
                    return int(first.get('id') or first.get('rider_id'))
        except Exception as e:
            log.warning(f"Library search failed for {name}: {e}")

    # Method 2: Direct HTTP search
    try:
        # Try full name first
        for query in [name, " ".join(reversed(name.split()))]:
            encoded = requests.utils.quote(query)
            url = f"https://firstcycling.com/search.php?q={encoded}&cat=rider"
            resp = requests.get(url, headers=FC_HEADERS, timeout=10, allow_redirects=True)

            if resp.status_code != 200:
                continue

            # Redirected directly to rider page
            if "rider.php?r=" in resp.url:
                m = re.search(r"r=(\d+)", resp.url)
                if m:
                    log.info(f"Found {name} via redirect: {m.group(1)}")
                    return int(m.group(1))

            soup = BeautifulSoup(resp.text, "html.parser")

            # Look for rider links in search results
            # FC search results have rider names as links
            for a in soup.find_all("a", href=re.compile(r"rider\.php\?r=\d+")):
                m = re.search(r"r=(\d+)", a["href"])
                if m:
                    log.info(f"Found {name} via search link: {m.group(1)}")
                    return int(m.group(1))

    except Exception as e:
        log.warning(f"HTTP search failed for {name}: {e}")

    return None


def get_results_via_library(rider_id: int) -> dict:
    """Use FirstCyclingAPI library to get results."""
    wins, podiums, top10s, notable = 0, 0, 0, []
    try:
        rider = Rider(rider_id)
        for year in (2023, 2024, 2025):
            try:
                yr = rider.year_results(year)
                df = yr.results_df
                if df is None or df.empty:
                    continue
                for _, row in df.iterrows():
                    try:
                        pos_raw = str(row.get('Pos', '')).strip()
                        pos_raw = re.sub(r'[^\d]', '', pos_raw)
                        if not pos_raw:
                            continue
                        pos = int(pos_raw)
                        if pos == 0 or pos > 200:
                            continue
                        race = str(row.get('Race', '')).strip()
                        if not race or len(race) < 3:
                            continue
                        if pos <= 10:
                            top10s += 1
                            if pos <= 3:
                                podiums += 1
                                if pos == 1:
                                    wins += 1
                                    notable.append(f"🏆 {race} ({year})")
                                else:
                                    notable.append(f"P{pos} {race} ({year})")
                    except Exception:
                        continue
            except Exception as e:
                log.warning(f"Year results error {rider_id}/{year}: {e}")
    except Exception as e:
        log.warning(f"Library results error {rider_id}: {e}")
    return {"wins": wins, "podiums": podiums, "top10s": top10s, "notable": notable[:4]}


def get_results_via_http(rider_id: int) -> dict:
    """Direct HTTP scraping with correct column detection."""
    wins, podiums, top10s, notable = 0, 0, 0, []

    for year in (2023, 2024, 2025):
        try:
            url = f"https://firstcycling.com/rider.php?r={rider_id}&y={year}"
            resp = requests.get(url, headers=FC_HEADERS, timeout=10)
            if resp.status_code != 200:
                continue

            soup = BeautifulSoup(resp.text, "html.parser")

            # Find the correct results table by its thead
            results_table = None
            pos_idx, race_idx = None, None

            for table in soup.find_all("table"):
                thead = table.find("thead")
                if not thead:
                    continue
                ths = [th.get_text(strip=True) for th in thead.find_all(["th", "td"])]
                if "Pos" in ths:
                    results_table = table
                    pos_idx = ths.index("Pos")
                    # Race column is usually after a flag column
                    for i, h in enumerate(ths):
                        if h in ("Race", "Race name", "") and i > pos_idx:
                            race_idx = i
                            break
                    if race_idx is None:
                        race_idx = pos_idx + 2  # typical FC layout: Pos, flag, Race
                    break

            if not results_table:
                log.warning(f"No results table found for rider {rider_id} year {year}")
                continue

            log.info(f"Rider {rider_id} year {year}: pos_idx={pos_idx} race_idx={race_idx}")

            for row in results_table.find_all("tr"):
                cols = row.find_all("td")
                if not cols or len(cols) <= max(pos_idx, race_idx):
                    continue

                pos_text = re.sub(r'[^\d]', '', cols[pos_idx].get_text(strip=True))
                if not pos_text or len(pos_text) > 3:
                    continue
                pos = int(pos_text)
                if pos == 0 or pos > 200:
                    continue

                race = cols[race_idx].get_text(strip=True)
                if not race or len(race) < 4 or race.isdigit():
                    continue

                log.info(f"  pos={pos} race={race}")

                if pos <= 10:
                    top10s += 1
                    if pos <= 3:
                        podiums += 1
                        if pos == 1:
                            wins += 1
                            notable.append(f"🏆 {race} ({year})")
                        else:
                            notable.append(f"P{pos} {race} ({year})")

        except Exception as e:
            log.warning(f"HTTP results error {rider_id}/{year}: {e}")

    return {"wins": wins, "podiums": podiums, "top10s": top10s, "notable": notable[:4]}


def process_rider(rider: dict) -> dict:
    result = {**rider, "wins": None, "podiums": None, "top10s": None, "notable": [], "found": False}

    rider_id = find_rider_id(rider["name"])
    if not rider_id:
        log.info(f"✗ Not found: {rider['name']}")
        return result

    log.info(f"✓ {rider['name']} -> fc_id={rider_id}")

    if USE_LIBRARY:
        stats = get_results_via_library(rider_id)
    else:
        stats = get_results_via_http(rider_id)

    log.info(f"  W={stats['wins']} P={stats['podiums']} T10={stats['top10s']}")
    result.update({**stats, "found": True, "fc_id": rider_id})
    return result


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
            riders = extract_riders_from_image(b64, mime)
            all_riders.extend(riders)
        except Exception as e:
            return jsonify({"error": f"Failed to extract riders: {str(e)}"}), 500

    seen = set()
    unique_riders = []
    for r in all_riders:
        if r["name"] not in seen:
            seen.add(r["name"])
            unique_riders.append(r)

    if not unique_riders:
        return jsonify({"error": "No riders found in images"}), 400

    log.info(f"Processing {len(unique_riders)} riders (library={'yes' if USE_LIBRARY else 'no'})...")

    enriched = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(process_rider, r): r for r in unique_riders}
        for future in as_completed(futures):
            try:
                enriched.append(future.result())
            except Exception as e:
                log.warning(f"Future error: {e}")
                enriched.append(futures[future])

    found = [r for r in enriched if r.get("found")]
    not_found = [r for r in enriched if not r.get("found")]
    found.sort(key=lambda r: (-(r.get("podiums") or 0), -(r.get("wins") or 0), -(r.get("top10s") or 0)))

    top10 = (found + not_found)[:10]

    return jsonify({
        "total_riders": len(unique_riders),
        "found_on_fc": len(found),
        "rankings": top10
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
