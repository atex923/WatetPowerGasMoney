import json
import copy
import re
import ssl
import threading
import time
import tkinter as tk
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime
from html import unescape
from pathlib import Path
from typing import Any
from tkinter import messagebox, ttk


APP_TITLE = "水電Gas記帳"
APP_VERSION = "V0.1.7"
WINDOW_TITLE = f"{APP_TITLE} {APP_VERSION}"
PROGRAM_HISTORY = [
    ("V0.1.0", "初版：建立自來水、電力、瓦斯記帳分頁，加入自動儲存、排序、費用圖表與統一發票對獎。"),
    ("V0.1.1", "移除視窗內容區主標題，平均度數折線調整到圖表下半部。"),
    ("V0.1.2", "將總度數調整為計價度數，新增去年同期度數欄位。"),
    ("V0.1.3", "新增回復上一步功能，支援記錄修改、排序與批次對獎回復。"),
    ("V0.1.4", "計價度數大於去年同期度數時，在前三分頁表格以紅色標示。"),
    ("V0.1.5", "開啟程式時，對獎欄空白或未對獎的記錄會自動重新對獎。"),
    ("V0.1.6", "將程式歷史寫入程式碼，方便從單一檔案追蹤版本變更。"),
    ("V0.1.7", "整理 GitHub 首頁資料，將最新主程式移到 repo 根目錄。"),
]
DATA_FILE = Path(__file__).with_name("utility_records.json")
AWARDS_CACHE_FILE = Path(__file__).with_name("invoice_awards_cache.json")
MOF_APP_ID = "EINV4201907015417"
MOF_API = "https://api.einvoice.nat.gov.tw/PB2CAPIVAN/invapp/InvApp"
STATIC_SITE = "https://invoice.etax.nat.gov.tw/"
MAC_BG = "#f5f5f7"
MAC_PANEL = "#ffffff"
MAC_BORDER = "#d2d2d7"
MAC_TEXT = "#1d1d1f"
MAC_SECONDARY = "#6e6e73"
MAC_BLUE = "#007aff"
MAC_RED = "#ff3b30"

SERVICES = {
    "自來水": {"months": "even"},
    "電力": {"months": "odd"},
    "瓦斯": {"months": "even"},
}

PRIZES = {
    "special": ("特別獎", 10_000_000),
    "grand": ("特獎", 2_000_000),
    "first": ("頭獎", 200_000),
    "second": ("二獎", 40_000),
    "third": ("三獎", 10_000),
    "fourth": ("四獎", 4_000),
    "fifth": ("五獎", 1_000),
    "sixth": ("六獎", 200),
    "extra_sixth": ("增開六獎", 200),
}


@dataclass
class AwardPeriod:
    term: str
    label: str
    special: str
    grand: str
    first: list[str]
    extra_sixth: list[str]


@dataclass
class MatchResult:
    period: AwardPeriod
    prize_key: str
    matched_number: str
    suffix_length: int

    @property
    def prize_name(self) -> str:
        return PRIZES[self.prize_key][0]

    @property
    def amount(self) -> int:
        return PRIZES[self.prize_key][1]


def current_roc_year() -> int:
    return datetime.now().year - 1911


def current_month() -> int:
    return datetime.now().month


def default_invoice_period(month: int) -> str:
    start = month if month % 2 == 1 else month - 1
    start = max(1, min(11, start))
    return f"{start}-{start + 1}月份"


def parse_number(value: str, number_type=float):
    value = value.strip()
    if not value:
        return None
    return number_type(value)


def normalize_digits(value: str, length: int | None = None) -> str:
    digits = "".join(re.findall(r"\d", value))
    if length is not None and len(digits) != length:
        raise ValueError(f"需要 {length} 碼數字，收到：{value!r}")
    return digits


def clean_award_numbers(value: Any, *, number_len: int) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        raw = " ".join(str(item) for item in value)
    else:
        raw = str(value)
    return re.findall(rf"(?<!\d)\d{{{number_len}}}(?!\d)", raw)


def term_label(term: str) -> str:
    roc_year = int(term[:3])
    pair = int(term[3:])
    month_start = pair * 2 - 1
    return f"{roc_year}年{month_start:02d}-{month_start + 1:02d}月"


def term_from_invoice_period(year: int, invoice_period: str) -> str:
    match = re.search(r"(\d{1,2})\s*-\s*(\d{1,2})", invoice_period)
    if not match:
        raise ValueError("發票月份格式無法辨識")
    start_month = int(match.group(1))
    if start_month not in {1, 3, 5, 7, 9, 11}:
        raise ValueError("發票月份必須是 1-2、3-4、5-6、7-8、9-10、11-12")
    return f"{year:03d}{((start_month + 1) // 2):02d}"


def latest_announced_term() -> str:
    today = datetime.now().date()
    roc_year = today.year - 1911
    month = today.month
    if month % 2 == 1:
        pair = (month - 1) // 2 if today.day >= 25 else (month - 3) // 2
    else:
        pair = (month - 2) // 2
    if pair <= 0:
        roc_year -= 1
        pair += 6
    return f"{roc_year:03d}{pair:02d}"


def previous_term(term: str) -> str:
    year = int(term[:3])
    pair = int(term[3:]) - 1
    if pair == 0:
        year -= 1
        pair = 6
    return f"{year:03d}{pair:02d}"


def latest_terms(count: int = 3) -> list[str]:
    term = latest_announced_term()
    terms = []
    for _ in range(count):
        terms.append(term)
        term = previous_term(term)
    return terms


def fetch_json(url: str, params: dict[str, str] | None = None, timeout: int = 15) -> Any:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json,text/plain,*/*", "User-Agent": "Mozilla/5.0 utility-accounting/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8-sig", errors="replace")
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        if not isinstance(reason, ssl.SSLCertVerificationError):
            raise
        context = ssl._create_unverified_context()
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            body = response.read().decode("utf-8-sig", errors="replace")
    return json.loads(body)


def fetch_text(url: str, timeout: int = 15) -> str:
    request = urllib.request.Request(
        url,
        headers={"Accept": "text/html,application/xhtml+xml,*/*", "User-Agent": "Mozilla/5.0 utility-accounting/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8-sig", errors="replace")
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        if not isinstance(reason, ssl.SSLCertVerificationError):
            raise
        context = ssl._create_unverified_context()
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            return response.read().decode("utf-8-sig", errors="replace")


def strip_tags(html: str) -> str:
    html = re.sub(r"<script\b.*?</script>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<style\b.*?</style>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", html)
    return unescape(re.sub(r"\s+", " ", text))


def row_for_prize(html: str, prize_name: str) -> str:
    for row in re.findall(r"<tr\b.*?</tr>", html, flags=re.S | re.I):
        if re.search(rf">\s*{prize_name}\s*<", row):
            return row
    return ""


def p_big_numbers(row_html: str, number_len: int) -> list[str]:
    numbers = []
    blocks = re.findall(r"<p\b[^>]*etw-tbiggest[^>]*>(.*?)</p>", row_html, flags=re.S | re.I)
    for block in blocks:
        digits = normalize_digits(strip_tags(block))
        if len(digits) == number_len:
            numbers.append(digits)
    return numbers


def parse_static_award_page(html: str, expected_term: str) -> AwardPeriod:
    label = term_label(expected_term)
    special = p_big_numbers(row_for_prize(html, "特別獎"), 8)
    grand = p_big_numbers(row_for_prize(html, "特獎"), 8)
    first = p_big_numbers(row_for_prize(html, "頭獎"), 8)
    extra = p_big_numbers(row_for_prize(html, "增開六獎"), 3)
    if not special or not grand or not first:
        raise RuntimeError(f"無法從靜態頁辨識 {label} 的獎號")
    return AwardPeriod(expected_term, label, special[0], grand[0], first, extra)


def fetch_static_award_period(term: str) -> AwardPeriod:
    latest = latest_terms(2)
    if term not in latest:
        raise RuntimeError("靜態頁目前只提供最新兩期")
    page = "index.html" if term == latest[0] else "lastNumber.html"
    html = fetch_text(urllib.parse.urljoin(STATIC_SITE, page))
    return parse_static_award_page(html, term)


def parse_mof_payload(term: str, payload: dict[str, Any]) -> AwardPeriod:
    code = str(payload.get("code", ""))
    if code and code not in {"200", "0", "None"}:
        raise RuntimeError(f"財政部 API 回傳錯誤：{payload.get('msg', payload)}")

    def pick(*keys: str) -> Any:
        for key in keys:
            if key in payload and payload[key] not in (None, ""):
                return payload[key]
        return None

    special = clean_award_numbers(pick("superPrizeNo", "specialPrizeNo", "特別獎"), number_len=8)
    grand = clean_award_numbers(pick("spcPrizeNo", "grandPrizeNo", "特獎"), number_len=8)
    first = clean_award_numbers(pick("firstPrizeNo", "firstPrizeNos", "頭獎"), number_len=8)
    extra = clean_award_numbers(pick("sixthPrizeNo", "extraSixthPrizeNo", "增開六獎"), number_len=3)
    if not special or not grand or not first:
        raise RuntimeError(f"無法辨識 {term_label(term)} 的主要獎號")
    return AwardPeriod(term, term_label(term), special[0], grand[0], first, extra)


def fetch_award_period(term: str) -> AwardPeriod:
    try:
        return fetch_static_award_period(term)
    except Exception as static_exc:
        static_error = static_exc

    last_error = None
    for base in [
        {"version": "0.2", "action": "QryWinningList"},
        {"version": "0.2", "action": "qryWinningList"},
        {"version": "0.5", "action": "QryWinningList"},
    ]:
        params = {
            **base,
            "invTerm": term,
            "UUID": "00000000-0000-0000-0000-000000000000",
            "appID": MOF_APP_ID,
        }
        try:
            return parse_mof_payload(term, fetch_json(MOF_API, params))
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"抓取 {term_label(term)} 失敗：靜態頁：{static_error}；API：{last_error}")


def load_awards_cache() -> dict[str, AwardPeriod]:
    if not AWARDS_CACHE_FILE.exists():
        return {}
    with AWARDS_CACHE_FILE.open("r", encoding="utf-8") as file:
        raw = json.load(file)
    return {term: AwardPeriod(**item) for term, item in raw.items()}


def save_awards_cache(cache: dict[str, AwardPeriod]) -> None:
    data = {term: asdict(period) for term, period in sorted(cache.items(), reverse=True)}
    with AWARDS_CACHE_FILE.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)


def get_award_period(term: str, refresh: bool = False) -> AwardPeriod:
    cache = load_awards_cache()
    if refresh or term not in cache:
        cache[term] = fetch_award_period(term)
        save_awards_cache(cache)
        time.sleep(0.2)
    return cache[term]


def get_recent_awards(count: int = 3, refresh: bool = False) -> list[AwardPeriod]:
    periods = []
    for term in latest_terms(count):
        try:
            periods.append(get_award_period(term, refresh=refresh))
        except Exception:
            if refresh:
                raise
            cache = load_awards_cache()
            if term in cache:
                periods.append(cache[term])
    if not periods:
        raise RuntimeError("無法取得獎號，請確認網路連線。")
    return periods


def check_invoice(number: str, periods: list[AwardPeriod]) -> list[MatchResult]:
    invoice = normalize_digits(number, 8)
    results = []
    for period in periods:
        if invoice == period.special:
            results.append(MatchResult(period, "special", period.special, 8))
            continue
        if invoice == period.grand:
            results.append(MatchResult(period, "grand", period.grand, 8))
            continue

        best = None
        for first_no in period.first:
            for prize_key, suffix_length in [
                ("first", 8),
                ("second", 7),
                ("third", 6),
                ("fourth", 5),
                ("fifth", 4),
                ("sixth", 3),
            ]:
                if invoice[-suffix_length:] == first_no[-suffix_length:]:
                    candidate = MatchResult(period, prize_key, first_no, suffix_length)
                    if best is None or candidate.amount > best.amount:
                        best = candidate
                    break

        for extra_no in period.extra_sixth:
            if invoice[-3:] == extra_no:
                candidate = MatchResult(period, "extra_sixth", extra_no, 3)
                if best is None or candidate.amount > best.amount:
                    best = candidate

        if best:
            results.append(best)
    return sorted(results, key=lambda item: item.amount, reverse=True)


def format_awards(periods: list[AwardPeriod]) -> str:
    lines = []
    for period in periods:
        lines.append(f"{period.label} ({period.term})")
        lines.append(f"  特別獎：{period.special}")
        lines.append(f"  特獎  ：{period.grand}")
        lines.append(f"  頭獎  ：{', '.join(period.first)}")
        if period.extra_sixth:
            lines.append(f"  增開六獎：{', '.join(period.extra_sixth)}")
        lines.append("")
    return "\n".join(lines).strip()


def format_matches(invoice: str, matches: list[MatchResult]) -> str:
    if not matches:
        return f"{invoice}：未中獎"
    lines = [f"{invoice}：中獎"]
    for match in matches:
        lines.append(
            f"{match.period.label} {match.prize_name} NT${match.amount:,}\n"
            f"對中號碼 {match.matched_number}（後 {match.suffix_length} 碼）"
        )
    return "\n\n".join(lines)


def record_prize_text(record: dict[str, Any]) -> str:
    invoice = normalize_digits(str(record.get("invoice_number", "")), 8)
    term = term_from_invoice_period(int(record["year"]), str(record["invoice_period"]))
    award_period = get_award_period(term)
    matches = check_invoice(invoice, [award_period])
    if not matches:
        return "X"
    best = matches[0]
    return f"中獎 {best.prize_name} NT${best.amount:,}"


def configure_mac_style(root: tk.Tk) -> None:
    root.configure(bg=MAC_BG)
    style = ttk.Style(root)
    if "aqua" in style.theme_names():
        style.theme_use("aqua")
    elif "clam" in style.theme_names():
        style.theme_use("clam")

    style.configure(".", font=("TkDefaultFont", 12))
    style.configure("TFrame", background=MAC_BG)
    style.configure("TLabel", background=MAC_BG, foreground=MAC_TEXT)
    style.configure("TLabelframe", background=MAC_BG, bordercolor=MAC_BORDER)
    style.configure("TLabelframe.Label", background=MAC_BG, foreground=MAC_TEXT, font=("TkDefaultFont", 12, "bold"))
    style.configure("TNotebook", background=MAC_BG, borderwidth=0)
    style.configure("TNotebook.Tab", padding=(14, 7), font=("TkDefaultFont", 12))
    style.configure("Treeview", rowheight=26, background=MAC_PANEL, fieldbackground=MAC_PANEL, foreground=MAC_TEXT)
    style.configure("Treeview.Heading", font=("TkDefaultFont", 11, "bold"), foreground=MAC_TEXT)
    style.configure("TButton", padding=(12, 6))


class ChartCanvas(ttk.Frame):
    def __init__(self, master):
        super().__init__(master)
        self.canvas = tk.Canvas(self, bg=MAC_PANEL, highlightthickness=1, highlightbackground=MAC_BORDER)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _event: self.draw())
        self.records = []

    def set_records(self, records):
        self.records = records
        self.draw(records)

    def draw(self, records=None):
        if records is None:
            records = self.records

        canvas = self.canvas
        canvas.delete("all")
        width = max(canvas.winfo_width(), 360)
        height = max(canvas.winfo_height(), 260)
        left, right, top, bottom = 68, 68, 36, 54
        plot_w = width - left - right
        plot_h = height - top - bottom

        canvas.create_text(width / 2, 14, text="費用 / 平均度數折線圖", font=("TkDefaultFont", 13, "bold"), fill=MAC_TEXT)
        canvas.create_line(left, top, left, top + plot_h, fill=MAC_SECONDARY)
        canvas.create_line(left + plot_w, top, left + plot_w, top + plot_h, fill=MAC_SECONDARY)
        canvas.create_line(left, top + plot_h, left + plot_w, top + plot_h, fill=MAC_SECONDARY)
        average_top = top + plot_h * 0.52
        average_h = plot_h * 0.46

        canvas.create_text(18, average_top + average_h / 2, text="平均度數", angle=90, fill=MAC_BLUE)
        canvas.create_text(width - 18, top + plot_h / 2, text="費用", angle=270, fill=MAC_RED)
        canvas.create_text(left + plot_w / 2, height - 14, text="年/月", fill=MAC_TEXT)
        canvas.create_line(left + 10, 28, left + 42, 28, fill=MAC_BLUE, width=2)
        canvas.create_text(left + 48, 28, text="平均度數", anchor="w", fill=MAC_BLUE, font=("TkDefaultFont", 9))
        canvas.create_line(left + 118, 28, left + 150, 28, fill=MAC_RED, width=2)
        canvas.create_text(left + 156, 28, text="費用", anchor="w", fill=MAC_RED, font=("TkDefaultFont", 9))

        def numeric(record, key):
            value = record.get(key)
            if isinstance(value, (int, float)):
                return float(value)
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        usable = [
            r
            for r in records
            if numeric(r, "fee") is not None or numeric(r, "average_usage") is not None
        ]
        if not usable:
            canvas.create_text(width / 2, height / 2, text="尚無折線圖資料", fill=MAC_SECONDARY, font=("TkDefaultFont", 12))
            return

        fees = [numeric(r, "fee") for r in usable if numeric(r, "fee") is not None]
        averages = [numeric(r, "average_usage") for r in usable if numeric(r, "average_usage") is not None]
        min_fee = min(0, min(fees)) if fees else 0
        max_fee = max(fees) if fees else 1
        if max_fee == min_fee:
            max_fee += 1
        min_average = min(0, min(averages)) if averages else 0
        max_average = max(averages) if averages else 1
        if max_average == min_average:
            max_average += 1

        for index in range(5):
            ratio = index / 4
            y = top + plot_h - ratio * plot_h
            average_y = average_top + average_h - ratio * average_h
            average_value = min_average + ratio * (max_average - min_average)
            fee_value = min_fee + ratio * (max_fee - min_fee)
            canvas.create_line(left, y, left + plot_w, y, fill="#ececf0")
            canvas.create_text(left - 8, average_y, text=f"{average_value:.0f}", anchor="e", fill=MAC_BLUE, font=("TkDefaultFont", 9))
            canvas.create_text(left + plot_w + 8, y, text=f"{fee_value:.0f}", anchor="w", fill=MAC_RED, font=("TkDefaultFont", 9))

        def points_for(key, min_value, max_value, y_top, y_height):
            points = []
            for index, record in enumerate(usable):
                value = numeric(record, key)
                if value is None:
                    continue
                x = left + plot_w / 2 if len(usable) == 1 else left + index / (len(usable) - 1) * plot_w
                y = y_top + y_height - (value - min_value) / (max_value - min_value) * y_height
                points.append((x, y))
            return points

        average_points = points_for("average_usage", min_average, max_average, average_top, average_h)
        fee_points = points_for("fee", min_fee, max_fee, top, plot_h)

        def draw_line(points, color):
            if len(points) > 1:
                flattened = [coordinate for point in points for coordinate in point]
                canvas.create_line(*flattened, fill=color, width=2)
            for x, y in points:
                canvas.create_oval(x - 4, y - 4, x + 4, y + 4, fill=color, outline="")

        draw_line(average_points, MAC_BLUE)
        draw_line(fee_points, MAC_RED)

        label_step = max(1, len(usable) // 6)
        for index, record in enumerate(usable):
            if index % label_step == 0 or index == len(usable) - 1:
                x = left + plot_w / 2 if len(usable) == 1 else left + index / (len(usable) - 1) * plot_w
                canvas.create_text(x, top + plot_h + 16, text=f"{record['year']}/{record['month']:02d}", fill=MAC_SECONDARY, font=("TkDefaultFont", 9))


class UtilityTab(ttk.Frame):
    def __init__(self, master, app, service_name):
        super().__init__(master, padding=14)
        self.app = app
        self.service_name = service_name
        self.selected_index = None
        self.entries = {}
        self.status = tk.StringVar(value="")

        self.columnconfigure(0, weight=3, uniform="utility")
        self.columnconfigure(1, weight=2, uniform="utility")
        self.rowconfigure(1, weight=1)

        self.build_form()
        self.build_table()
        self.chart = ChartCanvas(self)
        self.chart.grid(row=0, column=1, rowspan=2, sticky="nsew", padx=(14, 0))
        self.refresh()

    def build_form(self):
        form = ttk.LabelFrame(self, text=f"{self.service_name}記帳", padding=12)
        form.grid(row=0, column=0, sticky="ew")

        fields = [
            ("year", "年份（中華民國）"),
            ("month", "月份"),
            ("fee", "費用"),
            ("total_usage", "度數"),
            ("last_year_usage", "去年同期度數"),
            ("average_usage", "平均度數"),
            ("invoice_number", "發票號碼"),
        ]

        defaults = {
            "year": str(current_roc_year()),
            "month": str(current_month()),
        }

        for index, (key, label_text) in enumerate(fields):
            ttk.Label(form, text=label_text).grid(row=index // 2, column=(index % 2) * 2, sticky="w", padx=(0, 8), pady=5)
            entry = ttk.Entry(form, width=18)
            entry.insert(0, defaults.get(key, ""))
            entry.grid(row=index // 2, column=(index % 2) * 2 + 1, sticky="ew", pady=5, padx=(0, 18))
            self.entries[key] = entry

        buttons = ttk.Frame(form)
        buttons.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        ttk.Button(buttons, text="新增記錄", command=self.add_record).pack(side="left", padx=(0, 6))
        ttk.Button(buttons, text="更新選取", command=self.update_record).pack(side="left", padx=(0, 6))
        ttk.Button(buttons, text="刪除選取", command=self.delete_record).pack(side="left", padx=(0, 6))
        ttk.Button(buttons, text="重新排序", command=self.sort_records).pack(side="left")
        ttk.Button(buttons, text="回復上一步", command=self.app.undo_last_action).pack(side="left", padx=(6, 0))
        ttk.Label(buttons, textvariable=self.status, foreground=MAC_BLUE).pack(side="left", padx=(12, 0))

        for col in range(4):
            form.columnconfigure(col, weight=1)

    def build_table(self):
        columns = (
            "year",
            "month",
            "fee",
            "total_usage",
            "last_year_usage",
            "average_usage",
            "invoice_number",
            "invoice_period",
            "prize_result",
        )
        labels = {
            "year": "年份",
            "month": "月份",
            "fee": "費用",
            "total_usage": "計價度數",
            "last_year_usage": "去年同期度數",
            "average_usage": "平均度數",
            "invoice_number": "發票號碼",
            "invoice_period": "發票月份",
            "prize_result": "對獎",
        }

        table_frame = ttk.Frame(self)
        table_frame.grid(row=1, column=0, sticky="nsew", pady=(14, 0))
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=12)
        for key in columns:
            self.tree.heading(key, text=labels[key])
            self.tree.column(key, anchor="center", width=92, minwidth=68, stretch=True)
        self.tree.column("invoice_number", width=126, stretch=True)
        self.tree.column("prize_result", width=158, stretch=True)
        self.tree.tag_configure("usage_increase", foreground=MAC_RED)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)

    def validate_month_rule(self, month):
        rule = SERVICES[self.service_name]["months"]
        if rule == "even" and month % 2 != 0:
            return f"{self.service_name}通常為偶數月記帳，仍可保存。"
        if rule == "odd" and month % 2 != 1:
            return f"{self.service_name}通常為奇數月記帳，仍可保存。"
        return ""

    def read_form(self):
        try:
            year = parse_number(self.entries["year"].get(), int)
            month = parse_number(self.entries["month"].get(), int)
            fee = parse_number(self.entries["fee"].get(), float)
            total_usage = parse_number(self.entries["total_usage"].get(), float)
            last_year_usage = parse_number(self.entries["last_year_usage"].get(), float)
            average_usage = parse_number(self.entries["average_usage"].get(), float)
        except ValueError:
            messagebox.showerror("格式錯誤", "年份、月份、費用、度數請輸入數字。")
            return None

        if year is None or month is None:
            messagebox.showerror("缺少資料", "請輸入年份與月份。")
            return None
        if not 1 <= month <= 12:
            messagebox.showerror("月份錯誤", "月份必須介於 1 到 12。")
            return None

        return {
            "year": year,
            "month": month,
            "fee": fee if fee is not None else 0,
            "total_usage": total_usage if total_usage is not None else 0,
            "last_year_usage": last_year_usage if last_year_usage is not None else 0,
            "average_usage": average_usage if average_usage is not None else 0,
            "invoice_number": self.entries["invoice_number"].get().strip(),
            "invoice_period": default_invoice_period(month),
            "prize_result": "",
        }

    def add_record(self):
        record = self.read_form()
        if not record:
            return
        self.app.push_undo_state()
        prize_message = self.app.apply_invoice_check(record)
        self.app.records[self.service_name].append(record)
        self.sort_and_save()
        self.status.set(prize_message or self.validate_month_rule(record["month"]) or "已新增並自動儲存")
        self.clear_selection(keep_date=True)

    def update_record(self):
        if self.selected_index is None:
            messagebox.showinfo("尚未選取", "請先在下方表格選取要更新的記錄。")
            return
        record = self.read_form()
        if not record:
            return
        self.app.push_undo_state()
        prize_message = self.app.apply_invoice_check(record)
        self.app.records[self.service_name][self.selected_index] = record
        self.sort_and_save()
        self.status.set(prize_message or self.validate_month_rule(record["month"]) or "已更新並自動儲存")

    def delete_record(self):
        if self.selected_index is None:
            messagebox.showinfo("尚未選取", "請先在下方表格選取要刪除的記錄。")
            return
        self.app.push_undo_state()
        del self.app.records[self.service_name][self.selected_index]
        self.selected_index = None
        self.sort_and_save()
        self.clear_selection(keep_date=True)
        self.status.set("已刪除並自動儲存")

    def sort_records(self):
        self.app.push_undo_state()
        self.sort_and_save()
        self.status.set("已依年份與月份重新排序")

    def sort_and_save(self):
        self.app.records[self.service_name].sort(key=lambda r: (r.get("year", 0), r.get("month", 0)))
        self.app.save_records()
        self.refresh()

    def refresh(self):
        for item in self.tree.get_children():
            self.tree.delete(item)

        for index, record in enumerate(self.app.records[self.service_name]):
            tags = ("usage_increase",) if self.is_usage_increased(record) else ()
            self.tree.insert(
                "",
                "end",
                iid=str(index),
                tags=tags,
                values=(
                    record.get("year", ""),
                    record.get("month", ""),
                    f"{record.get('fee', 0):.0f}" if isinstance(record.get("fee"), (int, float)) else record.get("fee", ""),
                    record.get("total_usage", ""),
                    record.get("last_year_usage", ""),
                    record.get("average_usage", ""),
                    record.get("invoice_number", ""),
                    record.get("invoice_period", ""),
                    record.get("prize_result", ""),
                ),
            )
        self.chart.set_records(self.app.records[self.service_name])

    def is_usage_increased(self, record):
        try:
            total_usage = float(record.get("total_usage", 0) or 0)
            last_year_usage = float(record.get("last_year_usage", 0) or 0)
        except (TypeError, ValueError):
            return False
        return last_year_usage > 0 and total_usage > last_year_usage

    def on_select(self, _event=None):
        selection = self.tree.selection()
        if not selection:
            return
        self.selected_index = int(selection[0])
        record = self.app.records[self.service_name][self.selected_index]
        for key, widget in self.entries.items():
            value = record.get(key, "")
            if isinstance(widget, ttk.Combobox):
                widget.set(value)
            else:
                readonly = str(widget.cget("state")) == "readonly"
                if readonly:
                    widget.configure(state="normal")
                widget.delete(0, "end")
                widget.insert(0, str(value))
                if readonly:
                    widget.configure(state="readonly")
        self.status.set("已載入選取記錄")

    def clear_selection(self, keep_date=False):
        self.tree.selection_remove(self.tree.selection())
        self.selected_index = None
        if not keep_date:
            return
        for key in ("fee", "total_usage", "last_year_usage", "average_usage", "invoice_number"):
            self.set_entry_text(key, "")
        self.entries["year"].delete(0, "end")
        self.entries["year"].insert(0, str(current_roc_year()))
        self.entries["month"].delete(0, "end")
        self.entries["month"].insert(0, str(current_month()))

    def set_entry_text(self, key, value):
        widget = self.entries[key]
        readonly = str(widget.cget("state")) == "readonly"
        if readonly:
            widget.configure(state="normal")
        widget.delete(0, "end")
        widget.insert(0, str(value))
        if readonly:
            widget.configure(state="readonly")


class InvoiceCheckerTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=14)
        self.app = app
        self.periods = []
        self.invoice_var = tk.StringVar()
        self.status_var = tk.StringVar(value="正在載入獎號...")

        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(2, weight=1)

        header = ttk.Frame(self)
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Label(header, text="統一發票對獎", font=("TkDefaultFont", 16, "bold")).pack(side="left")
        ttk.Button(header, text="重新抓取獎號", command=lambda: self.load_awards(refresh=True)).pack(side="right")
        ttk.Button(header, text="重新對全部記錄", command=self.recheck_all_records).pack(side="right", padx=(0, 8))

        input_row = ttk.Frame(self)
        input_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(14, 10))
        ttk.Label(input_row, text="發票號碼").pack(side="left")
        entry = ttk.Entry(input_row, textvariable=self.invoice_var, width=24, font=("TkDefaultFont", 14))
        entry.pack(side="left", padx=8)
        entry.bind("<Return>", lambda _event: self.check_current_invoice())
        ttk.Button(input_row, text="對獎", command=self.check_current_invoice).pack(side="left")
        ttk.Button(input_row, text="清除", command=self.clear_result).pack(side="left", padx=(8, 0))
        ttk.Label(input_row, textvariable=self.status_var, foreground=MAC_BLUE).pack(side="left", padx=(14, 0))

        result_frame = ttk.LabelFrame(self, text="對獎結果", padding=8)
        result_frame.grid(row=2, column=0, sticky="nsew", padx=(0, 7))
        result_frame.rowconfigure(0, weight=1)
        result_frame.columnconfigure(0, weight=1)
        self.result_text = tk.Text(
            result_frame,
            height=16,
            wrap="word",
            font=("TkDefaultFont", 13),
            bg=MAC_PANEL,
            fg=MAC_TEXT,
            highlightthickness=1,
            highlightbackground=MAC_BORDER,
            relief="flat",
        )
        self.result_text.grid(row=0, column=0, sticky="nsew")
        self.result_text.configure(state="disabled")

        awards_frame = ttk.LabelFrame(self, text="目前獎號", padding=8)
        awards_frame.grid(row=2, column=1, sticky="nsew", padx=(7, 0))
        awards_frame.rowconfigure(0, weight=1)
        awards_frame.columnconfigure(0, weight=1)
        self.awards_text = tk.Text(
            awards_frame,
            height=16,
            wrap="word",
            font=("Menlo", 12),
            bg=MAC_PANEL,
            fg=MAC_TEXT,
            highlightthickness=1,
            highlightbackground=MAC_BORDER,
            relief="flat",
        )
        self.awards_text.grid(row=0, column=0, sticky="nsew")
        self.awards_text.configure(state="disabled")

        self.load_awards(refresh=False)

    def set_text(self, widget, content):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("end", content)
        widget.configure(state="disabled")

    def load_awards(self, refresh=False):
        self.status_var.set("正在抓取獎號..." if refresh else "正在載入獎號...")
        self.set_text(self.result_text, "")

        def worker():
            try:
                periods = get_recent_awards(3, refresh=refresh)
            except Exception as exc:
                self.after(0, lambda: self.show_load_error(exc))
                return
            self.after(0, lambda: self.show_awards(periods))

        threading.Thread(target=worker, daemon=True).start()

    def show_load_error(self, exc):
        self.status_var.set("獎號載入失敗")
        self.set_text(self.awards_text, "")
        self.set_text(self.result_text, str(exc))

    def show_awards(self, periods):
        self.periods = periods
        self.status_var.set(f"已載入 {len(periods)} 期獎號")
        self.set_text(self.awards_text, format_awards(periods))

    def check_current_invoice(self):
        if not self.periods:
            self.set_text(self.result_text, "獎號尚未載入完成。")
            return
        try:
            invoice = normalize_digits(self.invoice_var.get(), 8)
        except ValueError as exc:
            self.set_text(self.result_text, str(exc))
            return
        self.invoice_var.set(invoice)
        self.set_text(self.result_text, format_matches(invoice, check_invoice(invoice, self.periods)))

    def clear_result(self):
        self.invoice_var.set("")
        self.set_text(self.result_text, "")

    def recheck_all_records(self):
        self.status_var.set("正在重新對全部記錄...")
        self.app.push_undo_state()

        def worker():
            updated = 0
            failed = 0
            for service_name in SERVICES:
                for record in self.app.records[service_name]:
                    if not record.get("invoice_number"):
                        continue
                    message = self.app.apply_invoice_check(record, quiet=True)
                    if message:
                        failed += 1
                    else:
                        updated += 1
            self.app.save_records()
            self.after(0, lambda: self.finish_recheck(updated, failed))

        threading.Thread(target=worker, daemon=True).start()

    def finish_recheck(self, updated, failed):
        for tab in self.app.tabs.values():
            tab.refresh()
        self.status_var.set(f"完成重新對獎：{updated} 筆，失敗 {failed} 筆")


class UtilityAccountingApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(WINDOW_TITLE)
        self.geometry("1180x720")
        self.minsize(980, 600)
        configure_mac_style(self)
        self.records = self.load_records()
        self.undo_stack = []

        main = ttk.Frame(self, padding=(14, 10, 14, 14))
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(0, weight=1)

        notebook = ttk.Notebook(main)
        notebook.grid(row=0, column=0, sticky="nsew")

        self.tabs = {}
        for service_name in SERVICES:
            tab = UtilityTab(notebook, self, service_name)
            notebook.add(tab, text=service_name)
            self.tabs[service_name] = tab

        self.invoice_tab = InvoiceCheckerTab(notebook, self)
        notebook.add(self.invoice_tab, text="統一發票對獎")
        self.after(200, self.check_blank_prize_results_on_startup)

    def load_records(self):
        if not DATA_FILE.exists():
            return {name: [] for name in SERVICES}
        try:
            with DATA_FILE.open("r", encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, json.JSONDecodeError):
            messagebox.showwarning("資料讀取失敗", "資料檔無法讀取，將使用空白資料。")
            return {name: [] for name in SERVICES}

        records = {name: data.get(name, []) for name in SERVICES}
        for name in records:
            for record in records[name]:
                record.setdefault("last_year_usage", "")
            records[name].sort(key=lambda r: (r.get("year", 0), r.get("month", 0)))
        return records

    def save_records(self):
        with DATA_FILE.open("w", encoding="utf-8") as file:
            json.dump(self.records, file, ensure_ascii=False, indent=2)

    def push_undo_state(self):
        self.undo_stack.append(copy.deepcopy(self.records))
        if len(self.undo_stack) > 20:
            self.undo_stack.pop(0)

    def undo_last_action(self):
        if not self.undo_stack:
            messagebox.showinfo("無法回復", "目前沒有可回復的上一步。")
            return
        self.records = self.undo_stack.pop()
        self.save_records()
        for tab in self.tabs.values():
            tab.selected_index = None
            tab.refresh()
            tab.status.set("已回復上一步")
        self.invoice_tab.status_var.set("已回復上一步")

    def check_blank_prize_results_on_startup(self):
        def should_retry_prize_check(record):
            result = str(record.get("prize_result", "")).strip()
            return record.get("invoice_number") and result in {"", "未對獎"}

        pending = [
            record
            for service_name in SERVICES
            for record in self.records[service_name]
            if should_retry_prize_check(record)
        ]
        if not pending:
            return

        self.invoice_tab.status_var.set(f"正在補對獎 {len(pending)} 筆舊記錄...")
        self.push_undo_state()

        def worker():
            updated = 0
            failed = 0
            for record in pending:
                message = self.apply_invoice_check(record, quiet=True)
                if message:
                    if message.startswith("對獎失敗"):
                        record["prize_result"] = ""
                    failed += 1
                else:
                    updated += 1
            self.save_records()
            self.after(0, lambda: self.finish_startup_prize_check(updated, failed))

        threading.Thread(target=worker, daemon=True).start()

    def finish_startup_prize_check(self, updated, failed):
        for tab in self.tabs.values():
            tab.refresh()
        self.invoice_tab.status_var.set(f"啟動補對獎完成：{updated} 筆，失敗 {failed} 筆")

    def apply_invoice_check(self, record, quiet=False):
        invoice_number = str(record.get("invoice_number", "")).strip()
        if not invoice_number:
            record["prize_result"] = ""
            return ""
        try:
            record["prize_result"] = record_prize_text(record)
            return ""
        except ValueError as exc:
            record["prize_result"] = "發票號碼需8碼"
            return str(exc)
        except Exception as exc:
            record["prize_result"] = "未對獎"
            return f"對獎失敗：{exc}"


if __name__ == "__main__":
    app = UtilityAccountingApp()
    app.mainloop()
