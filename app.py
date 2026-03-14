import os
import re
import time
import base64
import anthropic
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, render_template

app = Flask(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
FC_HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "nl-BE,nl;q=0.9,en;q=0.8",
    "Referer": "https://firstcycling.com/",
}


def extract_riders_from_image(image_b64: str, mime_type: str) -> list[dict]:
    """Use Claude vision to extract rider names from start list image."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4000,
        system="""You read Belgian cycling race start lists. Extract every rider.
Format is usually: NUMBER  SURNAME Firstname  TEAM  Category  UCI
Names are ALL-CAPS surname + capitalised firstname.
Keep Belgian prefixes (Van, De, Van den, Van der, Van de) with the surname.
Return ONLY valid JSON, nothing else:
{"riders":[{"bib":"55","name":"Firstname Surname","team":"Team Name"}]}
Convert to normal caps: VAN DEN BERGHE Nathan -> Nathan Van Den Berghe""",
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": image_b64}},
                {"type": "text", "text": "Extract all riders. Return only JSON."}
            ]
        }]
    )
    text = response.content[0].text.strip()
    # Strip markdown fences if present
    text = re.sub(r'^```json\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    import json
    data = json.loads(text)
    return data.get("riders", [])


def search_firstcycling(name: str) -> int | None:
    """Search FirstCycling for a rider, return their rider ID."""
    try:
        query = name.replace(" ", "+")
        url = f"https://firstcycling.com/search.php?q={query}&cat=rider"
        resp = requests.get(url, headers=FC_HEADERS, timeout=8)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        # First result link like /rider.php?r=12345
        link = soup.find("a", href=re.compile(r"/rider\.php\?r=\d+"))
        if link:
            m = re.search(r"r=(\d+)", link["href"])
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return None


def get_rider_results(rider_id: int, years=(2024, 2025)) -> dict:
    """Fetch rider results for given years, count podiums and top10s from 1.12B/kermesse races."""
    podiums = 0
    top10s = 0
    wins = 0
    notable = []

    for year in years:
        try:
            url = f"https://firstcycling.com/rider.php?r={rider_id}&y={year}"
            resp = requests.get(url, headers=FC_HEADERS, timeout=8)
            if resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            # Results are in a table
            table = soup.find("table", {"class": re.compile(r"tablesorter|results", re.I)})
            if not table:
                # Try any table with results
                tables = soup.find_all("table")
                for t in tables:
                    if t.find("td"):
                        table = t
                        break
            if not table:
                continue

            rows = table.find_all("tr")
            for row in rows:
                cols = row.find_all("td")
                if len(cols) < 3:
                    continue
                try:
                    pos_text = cols[1].get_text(strip=True) if len(cols) > 1 else ""
                    pos_text = re.sub(r'[^\d]', '', pos_text)
                    if not pos_text:
                        continue
                    pos = int(pos_text)
                    race_name = cols[2].get_text(strip=True) if len(cols) > 2 else ""

                    if pos <= 10:
                        top10s += 1
                        if pos <= 3:
                            podiums += 1
                            if pos == 1:
                                wins += 1
                                notable.append(f"🏆 {race_name} ({year})")
                            elif pos <= 3:
                                notable.append(f"P{pos} {race_name} ({year})")
                except (ValueError, IndexError):
                    continue
        except Exception:
            continue
        time.sleep(0.3)  # be polite

    return {
        "wins": wins,
        "podiums": podiums,
        "top10s": top10s,
        "notable": notable[:3]
    }


def prerank_by_knowledge(riders: list[dict]) -> list[str]:
    """Use Claude to pick top 20 candidates by knowledge."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    rider_list = "\n".join([f"- {r['bib']} {r['name']} ({r.get('team','')})" for r in riders])
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=800,
        messages=[{
            "role": "user",
            "content": f"""Belgian cycling expert. From this 1.12B start list, pick the 20 most likely strongest riders based on team quality and name recognition. 

{rider_list}

Strong teams to prioritise: Urbano-Vulsteke, VDM-Trawobo, Vetrapo, Shifting Gears, Debondt Verandas, HUBO-Scott, A.S. Construct-Castaar, Stageco, Van Eyck Sport-Josan, Baloise-Glowi Lions.

Reply ONLY with JSON: {{"top20":["Full Name","Full Name"]}}"""
        }]
    )
    text = response.content[0].text.strip()
    text = re.sub(r'^```json\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    import json
    data = json.loads(text)
    return data.get("top20", [])[:20]


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/analyze", methods=["POST"])
def analyze():
    import json

    files = request.files.getlist("images")
    if not files:
        return jsonify({"error": "No images uploaded"}), 400

    all_riders = []
    for f in files[:2]:
        raw = f.read()
        b64 = base64.b64encode(raw).decode()
        mime = f.content_type or "image/jpeg"
        # Resize if too large (> 3MB)
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

    # Deduplicate
    seen = set()
    unique_riders = []
    for r in all_riders:
        if r["name"] not in seen:
            seen.add(r["name"])
            unique_riders.append(r)

    if not unique_riders:
        return jsonify({"error": "No riders found in images"}), 400

    # Stream progress back via SSE would be ideal but for simplicity return JSON
    # Step 2: pre-rank by knowledge
    try:
        top20_names = prerank_by_knowledge(unique_riders)
    except Exception:
        top20_names = [r["name"] for r in unique_riders[:20]]

    # Build lookup dict for bibs
    bib_lookup = {r["name"]: r.get("bib", "?") for r in unique_riders}
    team_lookup = {r["name"]: r.get("team", "") for r in unique_riders}

    # Step 3: scrape FirstCycling for each of top 20
    results = []
    for name in top20_names:
        rider_id = search_firstcycling(name)
        if rider_id:
            stats = get_rider_results(rider_id, years=(2024, 2025))
        else:
            stats = {"wins": None, "podiums": None, "top10s": None, "notable": []}
        results.append({
            "bib": bib_lookup.get(name, "?"),
            "name": name,
            "team": team_lookup.get(name, ""),
            "wins": stats["wins"],
            "podiums": stats["podiums"],
            "top10s": stats["top10s"],
            "notable": stats["notable"],
            "found": rider_id is not None
        })
        time.sleep(0.2)

    # Sort by podiums desc, then top10s
    def sort_key(r):
        return (-(r["podiums"] or -1), -(r["top10s"] or -1))

    results.sort(key=sort_key)
    top10 = results[:10]

    return jsonify({
        "total_riders": len(unique_riders),
        "searched": len(top20_names),
        "rankings": top10
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
