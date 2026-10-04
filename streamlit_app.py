import os
import re
from datetime import datetime

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from PIL import Image, ImageOps
import base64
import html
import io
import json
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

st.set_page_config(page_title="Rick's Pub Finder", page_icon="🍺", layout="wide")

CSV_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "12pKG5ncN9IeNHR64w91M_a5r3s4z6DVVHSvNXQuGeM0/export?format=csv"
)
PHOTO_FOLDER = os.environ.get(
    "PUB_PHOTO_FOLDER",
    os.path.join(os.path.dirname(__file__), "photos"),
)
_NOMINATIM_LOCK = threading.Lock()
_NOMINATIM_LAST_REQUEST = 0.0


def configured_value(section, key, environment_variable):
    try:
        value = st.secrets.get(section, {}).get(key, "")
    except (FileNotFoundError, AttributeError):
        value = ""
    return value or os.environ.get(environment_variable, "")


def google_drive_settings():
    return (
        configured_value("google_drive", "folder_id", "GOOGLE_DRIVE_FOLDER_ID"),
        configured_value("google_drive", "api_key", "GOOGLE_DRIVE_API_KEY"),
    )


@st.cache_data(ttl=86400)
def load_data():
    return pd.read_csv(CSV_URL)


@st.cache_data
def prepare_data(df):
    df = df.dropna(subset=["Pub Name", "Locality"]).copy()
    df["Pub Name"] = df["Pub Name"].astype(str).str.strip()
    df["Locality"] = df["Locality"].astype(str).str.strip()
    df = df[(df["Pub Name"] != "") & (df["Locality"] != "")].copy()
    df["Date"] = pd.to_datetime(df["Date"], dayfirst=True, format="mixed", errors="coerce")
    df["_pub_norm"] = df["Pub Name"].map(normalise)
    df["_locality_norm"] = df["Locality"].map(normalise)
    return df


def normalise(value):
    """Make names and localities comparable despite punctuation or case."""
    value = str(value).lower().replace("-", " ")
    value = re.sub(r"[^a-z0-9 ]+", "", value)
    value = re.sub(r"\bthe\b", "", value)
    return " ".join(value.split())


def nominatim_contact_email():
    try:
        configured_email = st.secrets.get("nominatim", {}).get("contact_email", "")
    except FileNotFoundError:
        configured_email = ""
    return configured_email or os.environ.get("NOMINATIM_CONTACT_EMAIL", "")


@st.cache_data(persist="disk", show_spinner=False)
def geocode_location(query, contact_email):
    global _NOMINATIM_LAST_REQUEST

    params = urlencode(
        {
            "q": query,
            "format": "jsonv2",
            "limit": 1,
            "email": contact_email,
        }
    )
    request = Request(
        f"https://nominatim.openstreetmap.org/search?{params}",
        headers={
            "User-Agent": f"RickPubFinder/1.0 (contact: {contact_email})",
            "Accept": "application/json",
        },
    )

    with _NOMINATIM_LOCK:
        wait_seconds = 1.1 - (time.monotonic() - _NOMINATIM_LAST_REQUEST)
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        _NOMINATIM_LAST_REQUEST = time.monotonic()
        try:
            with urlopen(request, timeout=20) as response:
                locations = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise RuntimeError(f"Geocoder returned HTTP {exc.code}.") from exc
        except URLError as exc:
            raise RuntimeError(f"Could not reach the geocoder: {exc.reason}.") from exc

    if not locations:
        return None

    location = locations[0]
    return {"Latitude": float(location["lat"]), "Longitude": float(location["lon"])}


def build_map_locations(results):
    locations = {}
    for _, row in results.iterrows():
        pub_name = display_value(row["Pub Name"])
        locality = display_value(row["Locality"])
        address = display_value(row.get("Address", pd.NA))
        address_key = normalise(address) if address != "Not provided" else ""
        key = (normalise(pub_name), normalise(locality), address_key)

        if key in locations:
            locations[key]["Visits"] += 1
            continue

        query_parts = [pub_name]
        if address != "Not provided":
            query_parts.append(address)
        query_parts.append(locality)
        locations[key] = {
            "Key": json.dumps(key),
            "Pub Name": pub_name,
            "Locality": locality,
            "Address": address,
            "Visits": 1,
            "Query": ", ".join(dict.fromkeys(query_parts)),
        }

    return pd.DataFrame(locations.values())


def geocode_location_batch(locations, start, batch_size, contact_email, progress, status):
    mapped_rows = []
    not_found = []
    next_index = start
    end = min(start + batch_size, len(locations))

    for position in range(start, end):
        row = locations.iloc[position]
        status.text(f"Looking up {position + 1} of {len(locations)}: {row['Pub Name']}")

        try:
            coordinates = geocode_location(row["Query"], contact_email)
            if coordinates is None and row["Address"] != "Not provided":
                fallback_query = f"{row['Pub Name']}, {row['Locality']}"
                status.text(
                    f"No full-address match for {row['Pub Name']}; "
                    "trying the pub name and locality."
                )
                coordinates = geocode_location(fallback_query, contact_email)
        except (RuntimeError, ValueError, KeyError, OSError) as exc:
            return (
                pd.DataFrame(mapped_rows),
                pd.DataFrame(not_found),
                position,
                f"{row['Pub Name']}: {exc}",
            )

        next_index = position + 1
        progress.progress(next_index / len(locations))
        if coordinates is None:
            not_found.append(
                {
                    "Pub Name": row["Pub Name"],
                    "Locality": row["Locality"],
                    "Address": row["Address"],
                    "Reason": "No result from Nominatim",
                }
            )
            continue

        mapped_rows.append(
            {
                "Key": row["Key"],
                "Pub Name": display_value(row["Pub Name"]),
                "Locality": display_value(row["Locality"]),
                "Visits": int(row["Visits"]),
                **coordinates,
            }
        )

    return pd.DataFrame(mapped_rows), pd.DataFrame(not_found), next_index, None


def display_value(value):
    if pd.isna(value) or str(value).strip().lower() in {"", "nan", "none"}:
        return "Not provided"
    return str(value).strip()


def parse_photo_date(prefix):
    try:
        if re.fullmatch(r"\d{1,2}[-_.]\d{1,2}[-_.]\d{2,4}", prefix):
            day, month, year = re.split(r"[-_.]", prefix)
            if len(year) == 2:
                year = "20" + year
            return datetime(int(year), int(month), int(day)).date()

        if len(prefix) == 6:
            return datetime.strptime(prefix, "%d%m%y").date()
        if len(prefix) == 8:
            return datetime.strptime(prefix, "%d%m%Y").date()
    except ValueError:
        pass

    return None


@st.cache_data
def thumbnail_data_uri(photo_path, modified_time):
    with Image.open(photo_path) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        image.thumbnail((180, 110))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=75)

    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


@st.cache_data(ttl=86400)
def google_drive_thumbnail_data_uri(file_id, width):
    params = urlencode({"id": file_id, "sz": f"w{width}"})
    request = Request(
        f"https://drive.google.com/thumbnail?{params}",
        headers={
            "User-Agent": "RickPubFinder/1.0",
            "Accept": "image/*",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            image_data = response.read()
    except HTTPError as exc:
        raise RuntimeError(
            f"Google Drive thumbnail returned HTTP {exc.code}."
        ) from exc
    except URLError as exc:
        raise RuntimeError(
            f"Could not retrieve a Google Drive thumbnail: {exc.reason}."
        ) from exc

    encoded = base64.b64encode(image_data).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


@st.cache_data(ttl=3600)
def list_google_drive_photos(folder_id, api_key):
    files = []
    page_token = None

    while True:
        params = {
            "q": f"'{folder_id}' in parents and trashed = false",
            "fields": "nextPageToken,files(id,name,mimeType)",
            "pageSize": 1000,
            "key": api_key,
        }
        if page_token:
            params["pageToken"] = page_token

        request = Request(
            f"https://www.googleapis.com/drive/v3/files?{urlencode(params)}",
            headers={"Accept": "application/json"},
        )
        try:
            with urlopen(request, timeout=30) as response:
                page = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Google Drive returned HTTP {exc.code}: {detail}"
            ) from exc
        except URLError as exc:
            raise RuntimeError(f"Could not reach Google Drive: {exc.reason}.") from exc
        except (TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Could not read the Google Drive response: {exc}.") from exc

        files.extend(
            file
            for file in page.get("files", [])
            if file.get("mimeType", "").startswith("image/")
        )
        page_token = page.get("nextPageToken")
        if not page_token:
            break

    return sorted(files, key=lambda file: file["name"].lower())


def build_photo_index(photo_files):
    index = {}
    supported_extensions = (".jpg", ".jpeg", ".png", ".gif", ".webp")

    for photo in photo_files:
        filename = photo["name"]
        if not filename.lower().endswith(supported_extensions):
            continue

        match = re.match(
            r"^(\d{1,2}[-_.]\d{1,2}[-_.]\d{2,4}|\d{6}|\d{8})\s+(.+?)\s+-\s+(.+)$",
            filename,
        )
        if not match:
            continue

        visit_date = parse_photo_date(match.group(1))
        if not visit_date:
            continue

        pub_name = match.group(2).strip()
        locality = os.path.splitext(match.group(3))[0].strip()
        key = (visit_date, normalise(pub_name), normalise(locality))
        index.setdefault(key, []).append(photo)

    return index


def matching_photo(row):
    if pd.isna(row["Date"]):
        return None

    photo_key = (
        row["Date"].date(),
        row["_pub_norm"],
        row["_locality_norm"],
    )
    filenames = photo_index.get(photo_key, [])
    return filenames[0] if filenames else None


def photo_image_source(photo, width):
    if photo.get("id"):
        return google_drive_thumbnail_data_uri(photo["id"], width)

    path = photo["path"]
    return thumbnail_data_uri(path, os.path.getmtime(path))


def display_photo(photo, width=720):
    if photo.get("id"):
        source = photo_image_source(photo, width)
        st.markdown(
            f'<img src="{html.escape(source, quote=True)}" alt="Pub photo" '
            'loading="lazy" style="display:block;width:100%;height:auto;">',
            unsafe_allow_html=True,
        )
        return

    try:
        with Image.open(photo["path"]) as image:
            corrected_image = ImageOps.exif_transpose(image).copy()
        st.image(corrected_image, use_container_width=True)
    except (OSError, ValueError) as exc:
        st.warning(f"Couldn't display photo {os.path.basename(photo['path'])}: {exc}")


def render_visit_timeline(monthly_visits, results):
    chart_width = max(900, len(monthly_visits) * 20)
    chart_height = 320
    left, right, top, bottom = 48, 16, 20, 76
    plot_width = chart_width - left - right
    plot_height = chart_height - top - bottom
    slot_width = plot_width / max(len(monthly_visits), 1)
    max_visits = max(int(monthly_visits["Visits"].max()), 1)

    first_result_by_month = {}
    for card_position, (_, row) in enumerate(results.iterrows()):
        if pd.notna(row["Date"]):
            month = row["Date"].to_period("M")
            first_result_by_month.setdefault(month, card_position)

    chart_items = [
        f'<svg class="visits-chart" role="img" aria-label="Pub visits by month" '
        f'viewBox="0 0 {chart_width} {chart_height}" width="{chart_width}" '
        f'height="{chart_height}" xmlns="http://www.w3.org/2000/svg">',
        f'<line x1="{left}" y1="{top + plot_height}" x2="{chart_width - right}" '
        f'y2="{top + plot_height}" class="axis-line"/>',
    ]

    for tick in range(5):
        value = round(max_visits * tick / 4)
        y = top + plot_height - plot_height * tick / 4
        chart_items.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{chart_width - right}" '
            f'y2="{y:.1f}" class="grid-line"/>'
            f'<text x="{left - 8}" y="{y + 4:.1f}" class="axis-label" '
            f'text-anchor="end">{value}</text>'
        )

    for index, row in enumerate(monthly_visits.itertuples(index=False)):
        month = pd.Timestamp(row.Month).to_period("M")
        visits = int(row.Visits)
        bar_height = plot_height * visits / max_visits
        bar_x = left + index * slot_width + slot_width * 0.15
        bar_y = top + plot_height - bar_height
        bar_width = max(slot_width * 0.7, 2)
        month_key = month.strftime("%Y-%m")
        title = f"{month.strftime('%B %Y')}: {visits} visit{'s' if visits != 1 else ''}"
        bar = (
            f'<rect class="visit-bar" data-month="{month_key}" '
            f'x="{bar_x:.2f}" y="{bar_y:.2f}" width="{bar_width:.2f}" '
            f'height="{max(bar_height, 1):.2f}" rx="2"><title>'
            f'{html.escape(title)}</title></rect>'
        )
        card_position = first_result_by_month.get(month)
        if card_position is not None:
            chart_items.append(
                f'<a href="#pub-card-{card_position}" target="_top" '
                f'aria-label="{html.escape(title, quote=True)}">{bar}</a>'
            )
        else:
            chart_items.append(bar)

        if index % 3 == 0 or index == len(monthly_visits) - 1:
            label_x = left + index * slot_width + slot_width * 0.5
            label_y = top + plot_height + 12
            chart_items.append(
                f'<text x="{label_x:.2f}" y="{label_y:.2f}" '
                f'class="month-label" text-anchor="end" '
                f'transform="rotate(-45 {label_x:.2f} {label_y:.2f})">'
                f'{month.strftime("%b %Y")}</text>'
            )

    chart_items.append("</svg>")

    carousel_items = []
    for position, (_, row) in reversed(list(enumerate(results.iterrows()))):
        photo = matching_photo(row)
        if not photo:
            continue

        image_uri = photo_image_source(photo, 300)
        pub_name = html.escape(display_value(row["Pub Name"]), quote=True)
        month_key = row["Date"].strftime("%Y-%m")
        carousel_items.append(
            f'<a href="#pub-card-{position}" target="_top" '
            f'data-month="{month_key}" aria-label="{pub_name}">'
            f'<img src="{html.escape(image_uri, quote=True)}" alt="{pub_name}" '
            f'loading="lazy"></a>'
        )

    markup = f"""
    <style>
    .timeline-panel {{
        padding: 14px 16px 12px;
        margin: 12px 0 20px;
        background: #fffaf0;
        border: 1px solid #d8cbaa;
        border-top: 4px solid #c69b52;
        border-radius: 10px;
        box-shadow: 0 4px 14px rgba(35, 69, 54, 0.12);
        color: #24352b;
        font-family: sans-serif;
    }}
    .timeline-panel h3 {{
        margin: 0 0 8px;
        color: #234536;
        font: 600 1.2rem Georgia, serif;
    }}
    .chart-scroll {{
        overflow-x: auto;
    }}
    .visits-chart {{
        display: block;
        max-width: none;
    }}
    .axis-line {{ stroke: #879487; stroke-width: 1; }}
    .grid-line {{ stroke: #e4ddce; stroke-width: 1; }}
    .axis-label, .month-label {{
        fill: #536357;
        font: 11px sans-serif;
    }}
    .visit-bar {{
        fill: #c69b52;
        transition: fill 0.15s ease;
    }}
    .visit-bar.is-visible {{ fill: #234536; }}
    .timeline-carousel {{
        display: flex;
        gap: 12px;
        overflow-x: auto;
        padding: 12px 2px 8px;
        scrollbar-color: #8a9b88 #eee8da;
    }}
    .timeline-carousel a {{
        flex: 0 0 150px;
        display: block;
    }}
    .timeline-carousel img {{
        display: block;
        width: 150px;
        height: 96px;
        object-fit: cover;
        border: 2px solid #fff;
        border-radius: 7px;
        box-shadow: 0 2px 7px rgba(0, 0, 0, 0.18);
        transition: transform 0.15s ease, border-color 0.15s ease;
    }}
    .timeline-carousel a:hover img {{
        transform: translateY(-2px);
        border-color: #c69b52;
    }}
    </style>
    <section class="timeline-panel">
        <h3>Pub visits by month</h3>
        <div class="chart-scroll">{''.join(chart_items)}</div>
        <div class="timeline-carousel">{''.join(carousel_items)}</div>
    </section>
    <script>
    const timeline = document.currentScript.parentElement;
    const carousel = timeline.querySelector(".timeline-carousel");
    const bars = timeline.querySelectorAll(".visit-bar");
    const visibleMonths = new Map();
    timeline.addEventListener("click", (event) => {{
        const link = event.target.closest('a[href^="#pub-card-"]');
        if (!link) return;

        event.preventDefault();
        const targetId = link.getAttribute("href").slice(1);
        try {{
            const target = window.parent.document.getElementById(targetId);
            if (target) {{
                target.scrollIntoView({{ behavior: "smooth", block: "start" }});
                window.parent.history.replaceState(null, "", `#${{targetId}}`);
            }} else {{
                console.error(`Pub card target not found: ${{targetId}}`);
            }}
        }} catch (error) {{
            console.error("Could not navigate to the pub card from the timeline.", error);
            window.parent.location.hash = targetId;
        }}
    }});
    const updateHighlights = () => {{
        const months = new Set(visibleMonths.values());
        bars.forEach((bar) => {{
            bar.classList.toggle("is-visible", months.has(bar.dataset.month));
        }});
    }};
    if (carousel && "IntersectionObserver" in window) {{
        const observer = new IntersectionObserver((entries) => {{
            entries.forEach((entry) => {{
                const month = entry.target.dataset.month;
                if (entry.isIntersecting && entry.intersectionRatio >= 0.25) {{
                    visibleMonths.set(entry.target, month);
                }} else {{
                    visibleMonths.delete(entry.target);
                }}
            }});
            updateHighlights();
        }}, {{ root: carousel, threshold: [0, 0.25] }});
        carousel.querySelectorAll("a[data-month]").forEach((card) => observer.observe(card));
    }}
    </script>
    """
    components.html(markup, height=540, scrolling=False)


@st.dialog("Pub details", width="large")
def show_pub_details(row):
    photo = matching_photo(row)

    if photo:
        display_photo(photo)
    else:
        st.info("No photo is available for this pub.")

    st.markdown(f"## {display_value(row['Pub Name'])}")
    st.write(f"📍 **Locality:** {display_value(row['Locality'])}")

    if pd.notna(row["Date"]):
        st.write(f"**Visited:** {row['Date'].strftime('%d %B %Y')}")

    for column, label in (
        ("Address", "Address"),
        ("Beer", "Beer & brewery"),
        ("Notes", "Notes"),
    ):
        value = display_value(row.get(column, pd.NA))
        if value != "Not provided":
            st.write(f"**{label}:** {value}")

st.markdown(
    """
    <style>
    .stApp {
        background: #f5f1e7;
        color: #24352b;
    }
    .block-container {
        padding-top: 2rem;
        max-width: 1200px;
    }
    .masthead {
        padding: 1.5rem 1.8rem;
        margin-bottom: 1.5rem;
        background: #234536;
        border-bottom: 5px solid #c69b52;
        color: #fffaf0;
    }
    .masthead-kicker {
        color: #e0c58d;
        font-size: 0.8rem;
        letter-spacing: 0.18rem;
        text-transform: uppercase;
    }
    .masthead h1 {
        margin: 0.25rem 0;
        color: #fffaf0;
        font-family: Georgia, serif;
    }
    .masthead p {
        margin: 0;
        color: #e8e3d7;
    }
    h2, h3 {
        color: #234536;
        font-family: Georgia, serif;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    """
    <header class="masthead">
      <div class="masthead-kicker">Rick's Real Ale Pints</div>
      <h1>🍺 First Pints</h1>
      <p>Search and explore Rick's pubs first visits, pints and photos</p>
    </header>
    """,
    unsafe_allow_html=True,
)

try:
    df = load_data()
except Exception as exc:
    st.error(f"Could not load the pub list. Check your internet connection and sheet link. ({exc})")
    st.stop()

required_columns = {"Pub Name", "Locality", "Date"}
missing_columns = required_columns - set(df.columns)
if missing_columns:
    st.error(f"Missing required spreadsheet columns: {', '.join(sorted(missing_columns))}")
    st.stop()

# Prepare searchable pub records without exposing the raw spreadsheet.
df = prepare_data(df)

drive_folder_id, drive_api_key = google_drive_settings()
if bool(drive_folder_id) != bool(drive_api_key):
    st.error(
        "Google Drive photo settings are incomplete. Configure both "
        "`google_drive.folder_id` and `google_drive.api_key`."
    )
    st.stop()

if drive_folder_id:
    try:
        drive_photos = list_google_drive_photos(drive_folder_id, drive_api_key)
    except RuntimeError as exc:
        st.error(f"Could not list photos in Google Drive: {exc}")
        st.stop()
    if not drive_photos:
        st.warning("The configured Google Drive folder contains no supported image files.")
    photo_index = build_photo_index(drive_photos)
else:
    local_photos = []
    if os.path.isdir(PHOTO_FOLDER):
        local_photos = [
            {"name": filename, "path": os.path.join(PHOTO_FOLDER, filename)}
            for filename in os.listdir(PHOTO_FOLDER)
        ]
    photo_index = build_photo_index(local_photos)

# Build the finder controls.
st.subheader("Search pubs")
search_col, locality_col, year_col = st.columns([2, 1, 1])

with search_col:
    with st.container(border=True):
        search_text = st.text_input(
            "Pub name or locality",
            placeholder="Try a name or town",
        )

localities = ["All localities"] + sorted(df["Locality"].dropna().unique().tolist())
with locality_col:
    with st.container(border=True):
        selected_locality = st.selectbox("Locality", localities)

years = sorted(df["Date"].dropna().dt.year.unique(), reverse=True)
with year_col:
    with st.container(border=True):
        selected_year = st.selectbox(
            "Visit year",
            ["All years"] + [str(year) for year in years],
        )
results = df.copy()

if search_text.strip():
    query = normalise(search_text)
    results = results[
        results["_pub_norm"].str.contains(query, na=False)
        | results["_locality_norm"].str.contains(query, na=False)
    ]

if selected_locality != "All localities":
    results = results[results["Locality"] == selected_locality]

if selected_year != "All years":
    results = results[results["Date"].dt.year == int(selected_year)]

if "Date" in results.columns:
    results = results.sort_values("Date", ascending=False, na_position="last")

visit_dates = df["Date"].dropna()
current_month = pd.Timestamp.now().to_period("M")
if visit_dates.empty:
    st.info("No valid visit dates are available to chart.")
else:
    first_month = visit_dates.min().to_period("M")
    if first_month > current_month:
        st.info("There are no pub visits dated up to the current month.")
    else:
        months = pd.period_range(first_month, current_month, freq="M")
        monthly_visits = (
            visit_dates.dt.to_period("M")
            .value_counts()
            .reindex(months, fill_value=0)
            .rename_axis("Month")
            .rename("Visits")
            .reset_index()
        )
        monthly_visits["Month"] = monthly_visits["Month"].dt.to_timestamp()

        render_visit_timeline(monthly_visits, results)

st.caption("The map is temporarily disabled while app performance is reviewed.")

# ...existing code...
st.caption(f"{len(results)} pub visit{'s' if len(results) != 1 else ''} found")

if not results.empty:
    year_counts = (
        results["Date"].dropna().dt.year.value_counts().sort_index(ascending=False)
    )
    year_legend = "".join(
        f'<span class="year-chip">{year} <b>{count}</b></span>'
        for year, count in year_counts.items()
    )

if not drive_folder_id and not os.path.isdir(PHOTO_FOLDER):
    st.info("The photo folder isn't available on this computer, so pub details will display without photos.")

if results.empty:
    st.info("No pubs match those search options. Try a different name, locality, or year.")
else:
    rows = list(results.iterrows())

    for index in range(0, len(rows), 4):
        columns = st.columns(4)

        for offset, (column, (row_index, row)) in enumerate(
            zip(columns, rows[index:index + 4])
        ):
            card_position = index + offset
            with column:
                st.markdown(
                    f'<div id="pub-card-{card_position}"></div>',
                    unsafe_allow_html=True,
                )
                with st.container(border=True):
                    date_value = row["Date"]
                    photo = matching_photo(row)
                    if photo:
                        display_photo(photo)

                    st.markdown(f"### {display_value(row['Pub Name'])}")
                    st.write(f"📍 {display_value(row['Locality'])}")

                    if pd.notna(date_value):
                        st.write(f"**Visited:** {date_value.strftime('%d %B %Y')}")
                        address = display_value(row.get("Address", pd.NA))
                    beer = display_value(row.get("Beer", pd.NA))
                    notes = display_value(row.get("Notes", pd.NA))

                    if address != "Not provided":
                        st.write(f"**Address:** {address}")

                    if beer != "Not provided":
                        st.write(f"**Beer & brewery:** {beer}")

                    if notes != "Not provided":
                        st.write(f"**Notes:** {notes}")

                    if st.button(
                        "View details",
                        key=f"details_{row_index}",
                        use_container_width=True,
                    ):
                        show_pub_details(row)
