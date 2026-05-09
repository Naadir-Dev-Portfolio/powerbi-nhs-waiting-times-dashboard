"""
Download and curate official NHS England RTT waiting-times data.

Default build:
- National trend history from the latest RTT overview workbook.
- Provider/trust and specialty detail from monthly full CSV extracts from
  April 2024 onward.
- NHS region and ICB level commissioner detail from monthly commissioner
  workbooks from April 2024 onward.

Run from the repository root:
    python "Source Data/scripts/build_nhs_waiting_times_project.py"
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin

import pandas as pd
import requests


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = PROJECT_ROOT / "Source Data"
RAW_ROOT = SOURCE_ROOT / "raw"
CURATED_ROOT = SOURCE_ROOT / "curated"

RTT_PAGES = [
    "https://www.england.nhs.uk/statistics/statistical-work-areas/rtt-waiting-times/rtt-data-2023-24/",
    "https://www.england.nhs.uk/statistics/statistical-work-areas/rtt-waiting-times/rtt-data-2024-25/",
    "https://www.england.nhs.uk/statistics/statistical-work-areas/rtt-waiting-times/rtt-data-2025-26/",
]

DEFAULT_DETAIL_START = "2024-04-01"
HTTP_HEADERS = {
    "User-Agent": "portfolio-data-build/1.0 (+public official NHS England data)"
}

MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


@dataclass(frozen=True)
class SourceLink:
    dataset: str
    period: str
    period_start: date
    text: str
    url: str
    page_url: str


def ensure_dirs() -> None:
    for path in [
        RAW_ROOT / "rtt_full_extract_zips",
        RAW_ROOT / "rtt_commissioner_workbooks",
        RAW_ROOT / "rtt_overview",
        CURATED_ROOT,
    ]:
        path.mkdir(parents=True, exist_ok=True)


def request_text(url: str) -> str:
    response = requests.get(url, headers=HTTP_HEADERS, timeout=60)
    response.raise_for_status()
    return response.text


def extract_links(html: str, page_url: str) -> Iterable[tuple[str, str]]:
    pattern = re.compile(
        r"<a\b[^>]*href=[\"'](?P<href>[^\"']+)[\"'][^>]*>(?P<body>.*?)</a>",
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(html):
        href = urljoin(page_url, match.group("href"))
        text = re.sub(r"<[^>]+>", " ", match.group("body"))
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            yield text, href


def parse_period_from_text(text: str) -> tuple[str, date] | None:
    match = re.search(
        r"\b(Jan|January|Feb|February|Mar|March|Apr|April|May|Jun|June|Jul|July|"
        r"Aug|August|Sep|Sept|September|Oct|October|Nov|November|Dec|December)"
        r"\s*[- ]?\s*(\d{2}|\d{4})\b",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    month_name = match.group(1).lower()
    year_text = match.group(2)
    year = int(year_text)
    if year < 100:
        year += 2000
    month = MONTHS[month_name]
    return f"{year:04d}-{month:02d}", date(year, month, 1)


def collect_source_links() -> list[SourceLink]:
    links: dict[tuple[str, str], SourceLink] = {}
    for page_url in RTT_PAGES:
        print(f"Reading source page: {page_url}")
        html = request_text(page_url)
        for text, url in extract_links(html, page_url):
            dataset = None
            if "Full CSV data file" in text:
                dataset = "rtt_full_extract_zip"
            elif "Incomplete Commissioner" in text:
                dataset = "rtt_incomplete_commissioner"
            elif "RTT Overview Timeseries Including Estimates" in text:
                dataset = "rtt_overview_timeseries"
            if not dataset:
                continue
            parsed = parse_period_from_text(text)
            if not parsed:
                continue
            period, period_start = parsed
            key = (dataset, period)
            links[key] = SourceLink(dataset, period, period_start, text, url, page_url)
    return sorted(links.values(), key=lambda item: (item.dataset, item.period_start))


def safe_filename(link: SourceLink) -> str:
    suffix = Path(link.url.split("?")[0]).suffix.lower()
    base = f"{link.period}_{link.dataset}"
    if suffix not in {".zip", ".xlsx", ".xls"}:
        suffix = ".bin"
    return f"{base}{suffix}"


def download_file(link: SourceLink, target_dir: Path) -> Path:
    target = target_dir / safe_filename(link)
    if target.exists() and target.stat().st_size > 0:
        print(f"Using cached {target.name}")
        return target
    print(f"Downloading {link.text}")
    with requests.get(link.url, headers=HTTP_HEADERS, timeout=180, stream=True) as response:
        response.raise_for_status()
        temp = target.with_suffix(target.suffix + ".part")
        with temp.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)
        temp.replace(target)
    return target


def clean_number(value):
    if pd.isna(value) or value == "-":
        return pd.NA
    return value


def to_int_series(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series.replace("-", pd.NA), errors="coerce").fillna(0).astype("int64")


def to_float_series(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series.replace("-", pd.NA), errors="coerce")


def process_national_overview(path: Path) -> pd.DataFrame:
    raw = pd.read_excel(path, sheet_name="Full Time Series", header=None)
    data = raw.iloc[12:, [2, 3, 4, 6, 8, 10, 12, 14]].copy()
    data.columns = [
        "Date",
        "Median Wait Weeks",
        "92nd Percentile Wait Weeks",
        "Within 18 Weeks",
        "Within 18 Weeks Rate",
        "Over 18 Weeks",
        "Over 52 Weeks",
        "Over 52 Weeks Rate",
    ]
    data = data[data["Date"].notna()].copy()
    data["Date"] = pd.to_datetime(data["Date"], errors="coerce").dt.date
    data = data[data["Date"].notna()].copy()
    for col in [
        "Median Wait Weeks",
        "92nd Percentile Wait Weeks",
        "Within 18 Weeks",
        "Within 18 Weeks Rate",
        "Over 18 Weeks",
        "Over 52 Weeks",
        "Over 52 Weeks Rate",
    ]:
        data[col] = data[col].map(clean_number)
    data = data[data[["Within 18 Weeks", "Over 18 Weeks"]].notna().any(axis=1)].copy()
    numeric_count_cols = ["Within 18 Weeks", "Over 18 Weeks", "Over 52 Weeks"]
    for col in numeric_count_cols:
        data[col] = to_int_series(data[col])
    for col in [
        "Median Wait Weeks",
        "92nd Percentile Wait Weeks",
        "Within 18 Weeks Rate",
        "Over 52 Weeks Rate",
    ]:
        data[col] = to_float_series(data[col])
    data["Total Waiting List"] = data["Within 18 Weeks"] + data["Over 18 Weeks"]
    data["Over 18 Weeks Rate"] = data["Over 18 Weeks"] / data["Total Waiting List"].where(
        data["Total Waiting List"] != 0
    )
    data["Within 18 Weeks Target"] = 0.92
    data["Source"] = "NHS England RTT Overview Timeseries"
    return data[
        [
            "Date",
            "Total Waiting List",
            "Within 18 Weeks",
            "Over 18 Weeks",
            "Over 52 Weeks",
            "Within 18 Weeks Rate",
            "Over 18 Weeks Rate",
            "Over 52 Weeks Rate",
            "Median Wait Weeks",
            "92nd Percentile Wait Weeks",
            "Within 18 Weeks Target",
            "Source",
        ]
    ]


def week_bucket_columns(columns: Iterable[str]) -> dict[str, int]:
    buckets: dict[str, int] = {}
    for col in columns:
        match = re.match(r"Gt\s+(\d{2,3})\s+To\s+\d{2,3}\s+Weeks SUM 1", col)
        if match:
            buckets[col] = int(match.group(1))
        elif col == "Gt 104 Weeks SUM 1":
            buckets[col] = 104
    return buckets


def read_full_extract_from_zip(zip_path: Path) -> pd.DataFrame:
    with zipfile.ZipFile(zip_path) as archive:
        csv_names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if len(csv_names) != 1:
            raise ValueError(f"Expected one CSV in {zip_path}, found {csv_names}")
        csv_name = csv_names[0]
        with archive.open(csv_name) as handle:
            header = pd.read_csv(handle, nrows=0).columns.tolist()
        buckets = week_bucket_columns(header)
        usecols = [
            "Period",
            "Provider Parent Org Code",
            "Provider Parent Name",
            "Provider Org Code",
            "Provider Org Name",
            "RTT Part Description",
            "Treatment Function Code",
            "Treatment Function Name",
            "Patients with unknown clock start date",
            "Total All",
        ] + list(buckets)
        with archive.open(csv_name) as handle:
            df = pd.read_csv(handle, usecols=usecols, low_memory=False)
    for col in ["Total All", "Patients with unknown clock start date", *buckets.keys()]:
        df[col] = to_int_series(df[col])
    return df


def full_extract_period(zip_path: Path) -> date:
    with zipfile.ZipFile(zip_path) as archive:
        csv_name = next(name for name in archive.namelist() if name.lower().endswith(".csv"))
    match = re.search(r"(20\d{6})", csv_name)
    if not match:
        raise ValueError(f"Cannot parse period from {csv_name}")
    period_end = datetime.strptime(match.group(1), "%Y%m%d").date()
    return date(period_end.year, period_end.month, 1)


def add_wait_bands(df: pd.DataFrame) -> pd.DataFrame:
    buckets = week_bucket_columns(df.columns)
    band_specs = {
        "Waits Over 18 Weeks": 18,
        "Waits Over 52 Weeks": 52,
        "Waits Over 65 Weeks": 65,
        "Waits Over 78 Weeks": 78,
        "Waits Over 104 Weeks": 104,
    }
    band_values = {}
    for new_col, lower_bound in band_specs.items():
        cols = [col for col, bucket_start in buckets.items() if bucket_start >= lower_bound]
        band_values[new_col] = df[cols].sum(axis=1) if cols else 0
    return pd.concat([df, pd.DataFrame(band_values, index=df.index)], axis=1)


def process_full_extracts(paths: list[Path]) -> tuple[pd.DataFrame, pd.DataFrame]:
    provider_frames = []
    specialty_frames = []
    for zip_path in sorted(paths):
        period = full_extract_period(zip_path)
        print(f"Processing full extract {zip_path.name}")
        df = read_full_extract_from_zip(zip_path)
        df = add_wait_bands(df)
        df = df[df["RTT Part Description"].eq("Incomplete Pathways")].copy()
        df["Date"] = period
        common_group = [
            "Date",
            "Provider Parent Org Code",
            "Provider Parent Name",
            "Provider Org Code",
            "Provider Org Name",
        ]
        metrics = [
            "Total All",
            "Patients with unknown clock start date",
            "Waits Over 18 Weeks",
            "Waits Over 52 Weeks",
            "Waits Over 65 Weeks",
            "Waits Over 78 Weeks",
            "Waits Over 104 Weeks",
        ]
        provider = (
            df[df["Treatment Function Code"].eq("C_999")]
            .groupby(common_group, dropna=False)[metrics]
            .sum()
            .reset_index()
        )
        provider_frames.append(provider)
        specialty = (
            df[~df["Treatment Function Code"].eq("C_999")]
            .groupby(
                common_group + ["Treatment Function Code", "Treatment Function Name"],
                dropna=False,
            )[metrics]
            .sum()
            .reset_index()
        )
        specialty_frames.append(specialty)
    provider_all = pd.concat(provider_frames, ignore_index=True) if provider_frames else pd.DataFrame()
    specialty_all = pd.concat(specialty_frames, ignore_index=True) if specialty_frames else pd.DataFrame()
    rename_map = {
        "Provider Parent Org Code": "Provider Parent Code",
        "Provider Parent Name": "Provider Parent Name",
        "Provider Org Code": "Provider Code",
        "Provider Org Name": "Provider Name",
        "Treatment Function Code": "Specialty Code",
        "Treatment Function Name": "Specialty Name",
        "Total All": "Waiting List",
        "Patients with unknown clock start date": "Unknown Clock Start",
    }
    provider_all = provider_all.rename(columns=rename_map)
    specialty_all = specialty_all.rename(columns=rename_map)
    return provider_all, specialty_all


def period_from_link_file(path: Path) -> date:
    match = re.match(r"(\d{4})-(\d{2})_", path.name)
    if not match:
        raise ValueError(f"Cannot parse period from {path.name}")
    return date(int(match.group(1)), int(match.group(2)), 1)


def read_commissioner_sheet(path: Path, sheet_name: str) -> pd.DataFrame:
    df = pd.read_excel(path, sheet_name=sheet_name, header=13)
    df = df.dropna(how="all")
    df = df.loc[:, ~df.columns.astype(str).str.startswith("Unnamed: 0")]
    return df


def normalise_commissioner(df: pd.DataFrame, date_value: date, geography: str) -> pd.DataFrame:
    df = df.copy()
    code_col = f"{geography} Code"
    name_col = f"{geography} Name"
    if code_col not in df.columns or name_col not in df.columns:
        raise ValueError(f"Missing {code_col}/{name_col} in commissioner workbook")
    df = df[df["Treatment Function Code"].notna()].copy()
    wanted = [
        code_col,
        name_col,
        "Treatment Function Code",
        "Treatment Function",
        "Total number of incomplete pathways",
        "Total within 18 weeks",
        "% within 18 weeks",
        "Average (median) waiting time (in weeks)",
        "92nd percentile waiting time (in weeks)",
        "Total 52 plus weeks",
        "Total 78 plus weeks",
        "Total 65 plus weeks",
        "% 52 plus weeks",
    ]
    df = df[[col for col in wanted if col in df.columns]].copy()
    df["Date"] = date_value
    df["Geography Type"] = geography
    df = df.rename(
        columns={
            code_col: "Geography Code",
            name_col: "Geography Name",
            "Treatment Function Code": "Specialty Code",
            "Treatment Function": "Specialty Name",
            "Total number of incomplete pathways": "Waiting List",
            "Total within 18 weeks": "Within 18 Weeks",
            "% within 18 weeks": "Within 18 Weeks Rate",
            "Average (median) waiting time (in weeks)": "Median Wait Weeks",
            "92nd percentile waiting time (in weeks)": "92nd Percentile Wait Weeks",
            "Total 52 plus weeks": "Waits Over 52 Weeks",
            "Total 78 plus weeks": "Waits Over 78 Weeks",
            "Total 65 plus weeks": "Waits Over 65 Weeks",
            "% 52 plus weeks": "Over 52 Weeks Rate",
        }
    )
    count_cols = [
        "Waiting List",
        "Within 18 Weeks",
        "Waits Over 52 Weeks",
        "Waits Over 78 Weeks",
        "Waits Over 65 Weeks",
    ]
    for col in count_cols:
        if col in df.columns:
            df[col] = to_int_series(df[col])
    for col in [
        "Within 18 Weeks Rate",
        "Median Wait Weeks",
        "92nd Percentile Wait Weeks",
        "Over 52 Weeks Rate",
    ]:
        if col in df.columns:
            df[col] = to_float_series(df[col])
    return df


def process_commissioner_workbooks(paths: list[Path]) -> tuple[pd.DataFrame, pd.DataFrame]:
    frames = []
    for path in sorted(paths):
        print(f"Processing commissioner workbook {path.name}")
        period = period_from_link_file(path)
        for sheet, geography in [("Region", "Region"), ("ICB", "ICB")]:
            df = read_commissioner_sheet(path, sheet)
            frames.append(normalise_commissioner(df, period, geography))
    combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    total = combined[combined["Specialty Code"].eq("C_999")].copy()
    specialty = combined[~combined["Specialty Code"].eq("C_999")].copy()
    return total, specialty


def build_dimensions(
    national: pd.DataFrame,
    provider: pd.DataFrame,
    specialty_fact: pd.DataFrame,
    region: pd.DataFrame,
    region_specialty: pd.DataFrame,
) -> dict[str, pd.DataFrame]:
    min_date = min(
        pd.to_datetime(national["Date"]).min(),
        pd.to_datetime(provider["Date"]).min() if not provider.empty else pd.to_datetime(national["Date"]).min(),
        pd.to_datetime(region["Date"]).min() if not region.empty else pd.to_datetime(national["Date"]).min(),
    )
    max_date = max(
        pd.to_datetime(national["Date"]).max(),
        pd.to_datetime(provider["Date"]).max() if not provider.empty else pd.to_datetime(national["Date"]).max(),
        pd.to_datetime(region["Date"]).max() if not region.empty else pd.to_datetime(national["Date"]).max(),
    )
    dates = pd.date_range(min_date, max_date, freq="MS")
    dim_date = pd.DataFrame({"Date": dates.date})
    dim_date["Year"] = dates.year
    dim_date["Month Number"] = dates.month
    dim_date["Month Name"] = dates.strftime("%B")
    dim_date["Month Short"] = dates.strftime("%b")
    dim_date["Year Month"] = dates.strftime("%Y-%m")
    dim_date["Quarter"] = "Q" + (((dates.month - 1) // 3) + 1).astype(str)
    fy_start = dates.year.where(dates.month >= 4, dates.year - 1)
    dim_date["Financial Year"] = [f"{y}/{str(y + 1)[-2:]}" for y in fy_start]
    dim_date["Financial Month Number"] = ((dates.month - 4) % 12) + 1

    dim_provider = provider[
        ["Provider Code", "Provider Name", "Provider Parent Code", "Provider Parent Name"]
    ].drop_duplicates()
    dim_provider = dim_provider.sort_values(["Provider Name", "Provider Code"])

    specialty_cols = ["Specialty Code", "Specialty Name"]
    specialty_dim_frames = []
    if not specialty_fact.empty:
        specialty_dim_frames.append(specialty_fact[specialty_cols])
    if not region_specialty.empty:
        specialty_dim_frames.append(region_specialty[specialty_cols])
    dim_specialty = pd.concat(specialty_dim_frames, ignore_index=True).drop_duplicates()
    dim_specialty = dim_specialty.sort_values(["Specialty Name", "Specialty Code"])

    dim_region = region[
        ["Geography Type", "Geography Code", "Geography Name"]
    ].drop_duplicates()
    dim_region["Region Key"] = dim_region["Geography Type"] + "|" + dim_region["Geography Code"].astype(str)
    dim_region = dim_region[["Region Key", "Geography Type", "Geography Code", "Geography Name"]]
    dim_region = dim_region.sort_values(["Geography Type", "Geography Name"])
    region["Region Key"] = region["Geography Type"] + "|" + region["Geography Code"].astype(str)
    region_specialty["Region Key"] = (
        region_specialty["Geography Type"] + "|" + region_specialty["Geography Code"].astype(str)
    )

    return {
        "dim_date": dim_date,
        "dim_provider": dim_provider,
        "dim_specialty": dim_specialty,
        "dim_region": dim_region,
    }


def write_csv(df: pd.DataFrame, filename: str) -> None:
    path = CURATED_ROOT / filename
    df.to_csv(path, index=False, quoting=csv.QUOTE_MINIMAL)
    print(f"Wrote {path.relative_to(PROJECT_ROOT)} ({len(df):,} rows)")


def write_manifest(links: list[SourceLink], downloaded: dict[str, list[Path]], detail_start: date) -> None:
    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "detail_start_month": detail_start.isoformat(),
        "source_pages": RTT_PAGES,
        "sources": [
            {
                "dataset": link.dataset,
                "period": link.period,
                "text": link.text,
                "url": link.url,
                "page_url": link.page_url,
            }
            for link in links
        ],
        "downloaded_files": {
            key: [str(path.relative_to(PROJECT_ROOT)) for path in paths]
            for key, paths in downloaded.items()
        },
    }
    (SOURCE_ROOT / "source_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def write_summary(national: pd.DataFrame, provider: pd.DataFrame, specialty: pd.DataFrame, region: pd.DataFrame) -> None:
    latest_date = pd.to_datetime(national["Date"]).max().date()
    latest_nat = national[pd.to_datetime(national["Date"]).dt.date.eq(latest_date)].iloc[0]
    previous_nat = national[pd.to_datetime(national["Date"]).dt.date.lt(latest_date)].tail(1)
    previous_total = int(previous_nat["Total Waiting List"].iloc[0]) if not previous_nat.empty else None
    latest_provider = provider[pd.to_datetime(provider["Date"]).dt.date.eq(latest_date)].copy()
    latest_provider["Long Wait Rate 52 Weeks"] = latest_provider["Waits Over 52 Weeks"] / latest_provider[
        "Waiting List"
    ].where(latest_provider["Waiting List"] != 0)
    top_providers = latest_provider.nlargest(10, "Waiting List")[
        ["Provider Code", "Provider Name", "Waiting List", "Waits Over 52 Weeks", "Long Wait Rate 52 Weeks"]
    ]
    latest_specialty = specialty[pd.to_datetime(specialty["Date"]).dt.date.eq(latest_date)].copy()
    top_specialties = latest_specialty.groupby(["Specialty Code", "Specialty Name"], as_index=False)[
        ["Waiting List", "Waits Over 52 Weeks", "Waits Over 65 Weeks", "Waits Over 78 Weeks"]
    ].sum()
    top_specialties = top_specialties.nlargest(10, "Waiting List")
    latest_region = region[pd.to_datetime(region["Date"]).dt.date.eq(latest_date)].copy()
    latest_region = latest_region[latest_region["Geography Type"].eq("Region")]
    latest_region = latest_region.nlargest(10, "Waiting List")[
        ["Geography Code", "Geography Name", "Waiting List", "Waits Over 52 Weeks", "Within 18 Weeks Rate"]
    ]
    summary = {
        "latest_month": latest_date.isoformat(),
        "national_waiting_list": int(latest_nat["Total Waiting List"]),
        "national_waiting_list_previous_month": previous_total,
        "national_waiting_list_month_change": int(latest_nat["Total Waiting List"] - previous_total)
        if previous_total is not None
        else None,
        "national_over_18_weeks": int(latest_nat["Over 18 Weeks"]),
        "national_over_52_weeks": int(latest_nat["Over 52 Weeks"]),
        "national_within_18_weeks_rate": float(latest_nat["Within 18 Weeks Rate"]),
        "top_providers_by_waiting_list": top_providers.to_dict(orient="records"),
        "top_specialties_by_waiting_list": top_specialties.to_dict(orient="records"),
        "regions_by_waiting_list": latest_region.to_dict(orient="records"),
    }
    (CURATED_ROOT / "latest_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--detail-start",
        default=DEFAULT_DETAIL_START,
        help="First month to download for full extract and commissioner detail, YYYY-MM-DD.",
    )
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="Reuse cached raw files and only rebuild curated CSVs.",
    )
    args = parser.parse_args()

    detail_start = datetime.strptime(args.detail_start, "%Y-%m-%d").date()
    ensure_dirs()

    downloaded: dict[str, list[Path]] = {"overview": [], "full_extract": [], "commissioner": []}
    if args.skip_download:
        links = []
        downloaded["overview"] = sorted((RAW_ROOT / "rtt_overview").glob("*.xlsx"))
        downloaded["full_extract"] = sorted((RAW_ROOT / "rtt_full_extract_zips").glob("*.zip"))
        downloaded["commissioner"] = sorted((RAW_ROOT / "rtt_commissioner_workbooks").glob("*.xlsx"))
    else:
        links = collect_source_links()
        overview_links = [link for link in links if link.dataset == "rtt_overview_timeseries"]
        full_links = [
            link
            for link in links
            if link.dataset == "rtt_full_extract_zip" and link.period_start >= detail_start
        ]
        commissioner_links = [
            link
            for link in links
            if link.dataset == "rtt_incomplete_commissioner" and link.period_start >= detail_start
        ]
        if not overview_links:
            raise RuntimeError("No RTT overview workbook links found.")
        latest_overview = max(overview_links, key=lambda item: item.period_start)
        downloaded["overview"] = [download_file(latest_overview, RAW_ROOT / "rtt_overview")]
        downloaded["full_extract"] = [
            download_file(link, RAW_ROOT / "rtt_full_extract_zips") for link in full_links
        ]
        downloaded["commissioner"] = [
            download_file(link, RAW_ROOT / "rtt_commissioner_workbooks") for link in commissioner_links
        ]

    if not downloaded["overview"] or not downloaded["full_extract"] or not downloaded["commissioner"]:
        raise RuntimeError("Missing raw input files. Run without --skip-download first.")

    national = process_national_overview(max(downloaded["overview"], key=lambda p: p.stat().st_mtime))
    provider, specialty = process_full_extracts(downloaded["full_extract"])
    region, region_specialty = process_commissioner_workbooks(downloaded["commissioner"])
    dims = build_dimensions(national, provider, specialty, region, region_specialty)

    write_csv(national, "fact_national_rtt_monthly.csv")
    write_csv(provider, "fact_provider_rtt_monthly.csv")
    write_csv(specialty, "fact_provider_specialty_rtt_monthly.csv")
    write_csv(region, "fact_region_rtt_monthly.csv")
    write_csv(region_specialty, "fact_region_specialty_rtt_monthly.csv")
    for name, df in dims.items():
        write_csv(df, f"{name}.csv")
    write_summary(national, provider, specialty, region)
    write_manifest(links, downloaded, detail_start)
    print("Build complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
