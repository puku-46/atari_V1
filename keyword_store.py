"""通知キーワードの保存先(Googleスプレッドシート)

アプリ(sweepstakes_app.py)で編集し、毎朝の通知(notify.py)で読み込みます。
スプレッドシートの「keywords」シートのA列に、1行に1キーワードで保存されます(1行目は見出し)。
"""
import gspread

SHEET_NAME = "keywords"
HEADER = "キーワード"


def clean_keywords(values) -> list[str]:
    """空白の整理・空欄の除去・重複の除去(順番は維持)。"""
    out, seen = [], set()
    for v in values:
        if v is None:
            continue
        k = " ".join(str(v).split())  # 前後と連続する空白(全角含む)を1つに整える
        if k and k.lower() != "nan" and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _worksheet(service_account_info: dict, spreadsheet_id: str):
    gc = gspread.service_account_from_dict(service_account_info)
    sh = gc.open_by_key(spreadsheet_id)
    try:
        return sh.worksheet(SHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=SHEET_NAME, rows=200, cols=2)
        ws.update(range_name="A1", values=[[HEADER]])
        return ws


def load_keywords(service_account_info: dict, spreadsheet_id: str) -> list[str]:
    ws = _worksheet(service_account_info, spreadsheet_id)
    return clean_keywords(ws.col_values(1)[1:])  # 1行目は見出し


def save_keywords(service_account_info: dict, spreadsheet_id: str, keywords) -> list[str]:
    ws = _worksheet(service_account_info, spreadsheet_id)
    kws = clean_keywords(keywords)
    ws.clear()
    ws.update(range_name="A1", values=[[HEADER]] + [[k] for k in kws])
    return kws
