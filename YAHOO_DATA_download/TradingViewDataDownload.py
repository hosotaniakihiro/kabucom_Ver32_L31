import os
import time
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager


# =====================================================================
# 保存ディレクトリ
# =====================================================================
SAVE_DIR = r"D:\tv_csv"
os.makedirs(SAVE_DIR, exist_ok=True)


# =====================================================================
# ダウンロード開始日
# =====================================================================
DOWNLOAD_START_DATES = {
    "1": "2025-09-01",
    "2": "2025-06-09",
    "3": "2025-03-10",
    "5": "2024-09-09",
}

SYMBOLS = ["TSE:9984"]
END_DATE = "2025-12-05"


# =====================================================================
# Chrome 起動
# =====================================================================
options = Options()
options.add_argument("--disable-blink-features=AutomationControlled")
options.add_argument("--start-maximized")
options.add_experimental_option("prefs", {
    "download.default_directory": SAVE_DIR,
    "download.prompt_for_download": False,
})

driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
wait = WebDriverWait(driver, 15)


# =====================================================================
# チャートへフォーカス（JSフォーカス）
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
    time.sleep(0.2)


# =====================================================================
# Alt+G → 移動パネルを開く
# =====================================================================
def open_jump_panel():
    print("📌 移動パネル OPEN")

    focus_chart()
    body = driver.find_element(By.TAG_NAME, "body")

    for _ in range(3):
        body.send_keys(Keys.LEFT_ALT, 'g')
        time.sleep(0.25)
        if len(driver.find_elements(By.ID, "CustomRange")) > 0:
            print("✅ Alt+G OPEN 成功")
            return

    print("⚠ Alt+G 失敗 → JS で開く")

    js = """
    const btn = document.querySelector('[aria-label="移動…"]');
    if (btn) btn.click();
    """
    driver.execute_script(js)
    time.sleep(0.6)


# =====================================================================
# CustomRange タブ
# =====================================================================
def select_custom_range():
    btn = wait.until(EC.element_to_be_clickable((By.ID, "CustomRange")))
    driver.execute_script("arguments[0].click();", btn)
    print("✅ CustomRange 選択")
    time.sleep(0.5)


# =====================================================================
# 移動ボタンを押す（最新 DOM 対応）
# =====================================================================
def click_move_button():
    selector = 'button[data-name="submit-button"][data-overflow-tooltip-text="移動"]'
    move_btn = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, selector)))

    for _ in range(40):
        if not move_btn.get_attribute("disabled"):
            driver.execute_script("arguments[0].click();", move_btn)
            print("✅ 移動ボタン クリック成功")
            time.sleep(0.5)
            return
        time.sleep(0.15)

    raise Exception("❌ 移動ボタンが有効にならない")


# =====================================================================
# 日付入力 → blur → 移動 → 再描画待ち
# =====================================================================
def set_date_range(start_date, end_date):
    print("📌 日付入力 → 移動")

    inputs = wait.until(
        EC.presence_of_all_elements_located((By.CSS_SELECTOR, 'input[placeholder="YYYY-MM-DD"]'))
    )
    start_box, end_box = inputs[0], inputs[1]

    canvas = driver.find_element(By.TAG_NAME, "canvas")
    before_h = canvas.get_attribute("height")

    # 入力
    start_box.click()
    start_box.send_keys(Keys.CONTROL, "a")
    start_box.send_keys(start_date)
    time.sleep(0.2)

    end_box.click()
    end_box.send_keys(Keys.CONTROL, "a")
    end_box.send_keys(end_date)
    time.sleep(0.2)

    driver.execute_script("arguments[0].blur();", end_box)
    time.sleep(0.3)

    click_move_button()

    # 再描画確認
    for _ in range(50):
        if canvas.get_attribute("height") != before_h:
            print("✅ チャート再描画 完了")
            return
        time.sleep(0.2)

    print("⚠ 再描画検知できず")


# =====================================================================
# ▼ save-load-menu を安定して開く関数（最重要）
# =====================================================================
def open_save_menu():
    print("📌 ▼ save-load-menu OPEN準備")

    btn = wait.until(
        EC.presence_of_element_located((By.CSS_SELECTOR, 'button[data-name="save-load-menu"]'))
    )

    # 1) 強制 Close
    driver.execute_script("arguments[0].click();", btn)
    time.sleep(0.4)

    # 2) 強制 Open（閉じていても開く、開いていても開き直す）
    driver.execute_script("arguments[0].click();", btn)
    time.sleep(0.6)

    print("✅ save-load-menu OPEN 完了")


# =====================================================================
# エクスポート
# =====================================================================
# =====================================================================
# エクスポート（iframe 対応 / 全 DOM サーチ版）
# =====================================================================
def export_csv():
    print("📌 エクスポート開始")

    # ▼ レイアウトメニュー ▼
    menu_btn = wait.until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, 'button[data-name="save-load-menu"]'))
    )
    driver.execute_script("arguments[0].click();", menu_btn)
    time.sleep(0.6)

    # Shadow DOM を再帰的に探索する JS
    find_export_js = """
        function deepQuerySelectorAll(element, selector) {
            let elements = [];
            function recurse(el) {
                try {
                    el.querySelectorAll(selector).forEach(e => elements.push(e));
                } catch(e) {}

                if (el.shadowRoot) {
                    recurse(el.shadowRoot);
                }
                el.children && Array.from(el.children).forEach(child => recurse(child));
            }
            recurse(element);
            return elements;
        }

        return deepQuerySelectorAll(document.body, 'span');
    """

    spans = driver.execute_script(find_export_js)

    export_item = None
    for s in spans:
        try:
            if "チャートデータをエクスポート" in s.text:
                export_item = s
                break
        except:
            pass

    if not export_item:
        raise Exception("❌ Shadow DOM 内に 'チャートデータをエクスポート' が見つかりません")

    # クリック
    driver.execute_script("arguments[0].click();", export_item)
    print("📌 エクスポートダイアログ表示")
    time.sleep(1)

    # ▼ UNIX 選択
    time_sel = wait.until(EC.element_to_be_clickable((By.ID, "time-format-select")))
    time_sel.click()
    time.sleep(0.4)

    unix_btn = wait.until(EC.element_to_be_clickable((By.ID, "time-format-unix")))
    unix_btn.click()
    time.sleep(0.4)

    # ▼ エクスポートボタン
    export_btn = wait.until(
        EC.element_to_be_clickable((By.XPATH, "//button[contains(text(),'エクスポート')]"))
    )
    driver.execute_script("arguments[0].click();", export_btn)

    print("🎉 CSV ダウンロード開始")
    time.sleep(3)





# =====================================================================
# Shadow DOM を含めた “全 DOM 検索” 関数（子孫再帰）
# =====================================================================
def deep_find_elements(selector):
    results = []

    # JS で shadow root を再帰的に探索し、全ての要素を返す
    js = """
    const selector = arguments[0];
    const results = [];

    function traverse(node) {
        if (!node) return;

        // 通常の querySelectorAll
        try {
            node.querySelectorAll(selector).forEach(e => results.push(e));
        } catch (e) {}

        // shadow root がある場合はさらに潜る
        if (node.shadowRoot) {
            traverse(node.shadowRoot);
        }

        // 子供を探索
        node.childNodes.forEach(n => traverse(n));
    }

    traverse(document);

    return results;
    """

    elements = driver.execute_script(js, selector)
    return elements


# =====================================================================
# メイン処理
# =====================================================================
for sym in SYMBOLS:
    for tf, start_date in DOWNLOAD_START_DATES.items():

        print("\n==============================")
        print(f"📌 {sym} / {tf}分足")
        print("==============================")

        url = f"https://jp.tradingview.com/chart/?symbol={sym}&interval={tf}"
        driver.get(url)
        time.sleep(5)

        focus_chart()
        open_jump_panel()
        select_custom_range()
        set_date_range(start_date, END_DATE)
        export_csv()

driver.quit()
print("\n🎉 全てのダウンロード完了！")
