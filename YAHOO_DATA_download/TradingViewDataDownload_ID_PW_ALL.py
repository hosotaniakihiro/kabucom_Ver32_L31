import os
import time
import pandas as pd
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager


# =====================================================================
# TradingView ログイン情報
# =====================================================================
TV_EMAIL = "hosotaniakihiro0708@gmail.com"
TV_PASSWORD = "Hanshin3214!@"


# =====================================================================
# Excel から銘柄コード読み取り
# =====================================================================
EXCEL_PATH = r"y:\kabu\data_j_tv.xls"

df = pd.read_excel(EXCEL_PATH, header=0)

# B列 = 2列目（0: A列, 1: B列）
codes = df.iloc[:, 1].dropna().astype(str).str.zfill(4).tolist()

SYMBOLS = [f"TSE:{c}" for c in codes]

print("読み取った銘柄数 =", len(SYMBOLS))



# =====================================================================
# ダウンロード設定
# =====================================================================
SAVE_DIR = r"D:\tv_csv"
os.makedirs(SAVE_DIR, exist_ok=True)

DOWNLOAD_START_DATES = {
    "1": "2025-09-01",
    "2": "2025-06-09",
    "3": "2025-03-10",
    "5": "2024-09-09",
}

END_DATE = "2025-12-10"


# =====================================================================
# Chrome 起動
# =====================================================================
options = Options()
options.add_argument("--start-maximized")
options.add_experimental_option("prefs", {
    "download.default_directory": SAVE_DIR,
    "download.prompt_for_download": False,
})

driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
wait = WebDriverWait(driver, 30)


# =====================================================================
# React 強制クリックメソッド（安全版）
# =====================================================================
def react_click(js_button_finder):
    js = """
        const btn = ({FINDER});
        if (!btn) return "NO_BUTTON";

        if (btn.disabled || btn.getAttribute("aria-disabled") === "true") {
            return "DISABLED";
        }

        const events = ["pointerdown", "mousedown", "pointerup", "mouseup", "click"];
        for (const ev of events) {
            btn.dispatchEvent(
                new MouseEvent(ev, {
                    view: window,
                    bubbles: true,
                    cancelable: true,
                    composed: true
                })
            );
        }
        return "CLICKED";
    """

    js = js.replace("{FINDER}", js_button_finder)

    for _ in range(20):
        res = driver.execute_script(js)
        print("▶ react_click:", res)
        if res == "CLICKED":
            return True
        time.sleep(0.1)

    return False


# =====================================================================
# TradingView ログイン
# =====================================================================
def login_tradingview():
    print("📌 TradingView ログイン開始")

    driver.get("https://www.tradingview.com/#signin")
    time.sleep(3)

    email_btn = wait.until(
        EC.element_to_be_clickable((By.XPATH, "//span[text()='Email' or contains(text(),'email')]"))
    )
    driver.execute_script("arguments[0].click();", email_btn)
    time.sleep(2)

    username = wait.until(EC.visibility_of_element_located((By.ID, "id_username")))
    username.clear()
    username.send_keys(TV_EMAIL)

    password = wait.until(EC.visibility_of_element_located((By.ID, "id_password")))
    password.clear()
    password.send_keys(TV_PASSWORD)

    btn = wait.until(
        EC.element_to_be_clickable((By.XPATH, "//button[contains(., 'Sign in')]"))
    )
    driver.execute_script("arguments[0].click();", btn)

    print("🎉 TradingView ログイン成功！")
    time.sleep(30)


# =====================================================================
# チャート操作
# =====================================================================
def focus_chart():
    js = """
    const el =
      document.querySelector('.chart-container') ||
      document.querySelector('[data-name="pane-root"]') ||
      document.querySelector('[data-name="chart-content"]');
    if (el) el.focus();
    """
    driver.execute_script(js)


def open_jump_panel():
    print("📌 Alt+G → 移動パネル開く")

    focus_chart()
    body = driver.find_element(By.TAG_NAME, "body")

    for _ in range(4):
        body.send_keys(Keys.LEFT_ALT, 'g')
        time.sleep(0.3)
        if driver.find_elements(By.ID, "CustomRange"):
            print("✅ Alt+G 成功")
            return

    driver.execute_script("const b=document.querySelector('[aria-label=\"移動…\"]'); if(b) b.click();")
    time.sleep(1)


def select_custom_range():
    btn = wait.until(EC.element_to_be_clickable((By.ID, "CustomRange")))
    driver.execute_script("arguments[0].click();", btn)
    time.sleep(0.5)


def click_move_button():
    selector = 'button[data-name="submit-button"][data-overflow-tooltip-text="移動"]'
    btn = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, selector)))
    time.sleep(0.2)

    driver.execute_script("arguments[0].click();", btn)
    print("✅ 移動ボタン クリック成功")


def set_date_range(start_date, end_date):
    print("📌 日付入力 → 移動")

    boxes = wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, 'input[placeholder="YYYY-MM-DD"]')))
    start_box, end_box = boxes

    start_box.click()
    start_box.send_keys(Keys.CONTROL, "a")
    start_box.send_keys(start_date)

    end_box.click()
    end_box.send_keys(Keys.CONTROL, "a")
    end_box.send_keys(end_date)

    time.sleep(0.3)
    click_move_button()


# =====================================================================
# ISO日時選択
# =====================================================================
def select_iso_timestamp():
    print("📌 ISO日時選択開始")

    js = """
    function deepFind(node, txt) {
        if (!node) return null;
        try {
            if (node.innerText && node.innerText.trim() === txt) return node;
        } catch(e){}
        if (node.shadowRoot) {
            const r = deepFind(node.shadowRoot, txt);
            if (r) return r;
        }
        for (const c of node.children || []) {
            const r = deepFind(c, txt);
            if (r) return r;
        }
        return null;
    }

    const dd = document.querySelector('#time-format-select');
    if (dd) dd.click();

    const found = deepFind(document.body, "ISO日時");
    if (found) { found.click(); return true; }

    return false;
    """

    for _ in range(10):
        if driver.execute_script(js):
            print("✅ ISO日時 選択完了")
            return
        time.sleep(0.4)

    raise Exception("❌ ISO日時 が見つからない")


# =====================================================================
# ダウンロード強制クリック
# =====================================================================
def force_download_click():
    for _ in range(20):
        ok = react_click("Array.from(document.querySelectorAll('button')).find(b => b.innerText==='ダウンロード')")
        if ok:
            print("🎉 ダウンロード押下成功！")
            return
        time.sleep(0.2)

    raise Exception("❌ ダウンロード押下に失敗")


# =====================================================================
# エクスポート → ダウンロード処理
# =====================================================================
def export_csv_iso():
    print("📌 エクスポート開始（ISO対応版）")

    # メニュー
    menu_btn = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, 'button[data-name="save-load-menu"]')))
    driver.execute_script("arguments[0].click();", menu_btn)
    time.sleep(0.5)

    # ダウンロード選択
    item = wait.until(EC.element_to_be_clickable((By.XPATH, "//span[contains(text(),'チャートデータをダウンロード')]")))
    driver.execute_script("arguments[0].click();", item)
    time.sleep(1)

    # ISO日時
    select_iso_timestamp()

    # ダウンロード
    force_download_click()


# =====================================================================
# 実行開始
# =====================================================================
login_tradingview()

for sym in SYMBOLS:

    print("\n======================================")
    print("📌 銘柄処理開始：", sym)
    print("======================================")

    for tf, start_date in DOWNLOAD_START_DATES.items():

        print(f"\n▶ {sym} / {tf}分足")

        url = f"https://jp.tradingview.com/chart/?symbol={sym}&interval={tf}"
        driver.get(url)
        time.sleep(10)

        focus_chart()
        open_jump_panel()
        select_custom_range()
        set_date_range(start_date, END_DATE)

        export_csv_iso()

        print("⏳ 5秒待機（次の時間足へ）")
        time.sleep(10)

print("\n🎉 全てのダウンロード完了！")
driver.quit()
