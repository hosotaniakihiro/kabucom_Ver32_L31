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
# TradingView のログイン情報
# =====================================================================
TV_EMAIL = "hosotaniakihiro0708@gmail.com"
TV_PASSWORD = "Hanshin3214!@"


# =====================================================================
# ダウンロード先
# =====================================================================
SAVE_DIR = r"D:\tv_csv"
os.makedirs(SAVE_DIR, exist_ok=True)


# =====================================================================
# 日付指定
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
# Chrome 起動（新規プロファイル）
# =====================================================================
options = Options()
options.add_argument("--start-maximized")
options.add_experimental_option("prefs", {
    "download.default_directory": SAVE_DIR,
    "download.prompt_for_download": False,
})

driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
wait = WebDriverWait(driver, 20)




def login_tradingview():
    print("📌 TradingView ログイン開始")

    driver.get("https://www.tradingview.com/#signin")
    time.sleep(3)

    # Email ログイン画面へ
    print("📌 Email ログイン画面へ移動")
    email_btn = wait.until(
        EC.element_to_be_clickable((By.XPATH, "//span[text()='Email' or contains(text(),'email')]"))
    )
    driver.execute_script("arguments[0].click();", email_btn)
    time.sleep(2)

    # Email input （表示されているものだけ）
    username_candidates = driver.find_elements(By.ID, "id_username")
    username_visible = [el for el in username_candidates if el.is_displayed()]
    if not username_visible:
        raise Exception("❌ 表示されている Email 入力欄が見つかりません")

    username_box = username_visible[0]
    username_box.clear()
    username_box.send_keys(TV_EMAIL)   # ← あなたのID
    print("✅ Email 入力成功")
    time.sleep(0.5)

    # Password input （表示されているものだけ）
    password_candidates = driver.find_elements(By.ID, "id_password")
    password_visible = [el for el in password_candidates if el.is_displayed()]
    if not password_visible:
        raise Exception("❌ 表示されている Password 入力欄が見つかりません")

    password_box = password_visible[0]
    password_box.clear()
    password_box.send_keys(TV_PASSWORD)   # ← あなたのPW
    print("✅ Password 入力成功")
    time.sleep(0.5)
    click_signin_button()

    print("🎉 TradingView ログイン成功！")
    time.sleep(3)
    # Sign in ボタン

    time.sleep(5)
    print("🎉 TradingView ログイン成功！")






# =====================================================================
# チャートにフォーカス
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
# Alt+G で移動パネル
# =====================================================================
def open_jump_panel():
    print("📌 移動パネル OPEN")

    focus_chart()
    body = driver.find_element(By.TAG_NAME, "body")

    for _ in range(3):
        body.send_keys(Keys.LEFT_ALT, 'g')
        time.sleep(0.3)
        if driver.find_elements(By.ID, "CustomRange"):
            print("✅ Alt+G 成功")
            return

    print("⚠ Alt+G 失敗 → JS 代替")
    js = "const b=document.querySelector('[aria-label=\"移動…\"]'); if(b) b.click();"
    driver.execute_script(js)
    time.sleep(0.6)


# =====================================================================
# カスタム範囲
# =====================================================================
def select_custom_range():
    btn = wait.until(EC.element_to_be_clickable((By.ID, "CustomRange")))
    driver.execute_script("arguments[0].click();", btn)
    print("✅ CustomRange 選択")
    time.sleep(0.5)


# =====================================================================
# 移動ボタン
# =====================================================================
def click_move_button():
    selector = 'button[data-name="submit-button"][data-overflow-tooltip-text="移動"]'
    move_btn = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, selector)))

    for _ in range(40):
        if not move_btn.get_attribute("disabled"):
            driver.execute_script("arguments[0].click();", move_btn)
            print("✅ 移動ボタン クリック成功")
            time.sleep(0.4)
            return
        time.sleep(0.15)

    raise Exception("❌ 移動ボタンが有効にならない")


# =====================================================================
# 日付入力
# =====================================================================
def set_date_range(start_date, end_date):
    print("📌 日付入力 → 移動")

    inputs = wait.until(
        EC.presence_of_all_elements_located((By.CSS_SELECTOR, 'input[placeholder="YYYY-MM-DD"]'))
    )

    start_box, end_box = inputs[0], inputs[1]

    start_box.click()
    start_box.send_keys(Keys.CONTROL, "a")
    start_box.send_keys(start_date)

    end_box.click()
    end_box.send_keys(Keys.CONTROL, "a")
    end_box.send_keys(end_date)

    time.sleep(0.3)
    driver.execute_script("arguments[0].blur();", end_box)

    click_move_button()


# =====================================================================
# ▼ save-load-menu → エクスポート
# =====================================================================
def export_csv():
    print("📌 エクスポート開始")

    menu_btn = wait.until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, 'button[data-name="save-load-menu"]'))
    )
    driver.execute_script("arguments[0].click();", menu_btn)
    time.sleep(0.6)

    # --- Shadow DOM も iframe も全部探索する JS ---
    deep_find = """
    function deep(root){
        let out=[];
        function walk(n){
            if(!n) return;
            try{ n.querySelectorAll("span").forEach(e=>out.push(e)); }catch(e){}
            if(n.shadowRoot) walk(n.shadowRoot);
            n.childNodes.forEach(c=>walk(c));
        }
        walk(root);
        return out;
    }
    return deep(document.body);
    """

    spans = driver.execute_script(deep_find)

    export_item = None
    for s in spans:
        try:
            if "チャートデータをダウンロード" in s.text:
                export_item = s
                break
        except:
            pass

    if not export_item:
        raise Exception("❌ 'チャートデータをダウンロード' が見つかりません")

    driver.execute_script("arguments[0].click();", export_item)
    print("📌 ダウンロードダイアログ OK")
    time.sleep(1)

    # UNIX 選択
    time_sel = wait.until(EC.element_to_be_clickable((By.ID, "time-format-select")))
    time_sel.click()
    time.sleep(0.3)

    unix_btn = wait.until(EC.element_to_be_clickable((By.ID, "time-format-unix")))
    unix_btn.click()
    time.sleep(0.3)

    # エクスポート
    btn = wait.until(
        EC.element_to_be_clickable((By.XPATH, "//button[contains(text(),'エクスポート')]"))
    )
    driver.execute_script("arguments[0].click();", btn)

    print("🎉 CSV ダウンロード開始")
    time.sleep(3)

def click_email_button():
    print("📌 Email ログインボタンを探します...")

    # Shadow DOM + iframe フル探索
    deep_js = """
    const results = [];
    function traverse(node) {
        if (!node) return;

        try {
            if (node.querySelectorAll) {
                node.querySelectorAll('button, div').forEach(e => results.push(e));
            }
        } catch(e) {}

        if (node.shadowRoot) traverse(node.shadowRoot);
        if (node.contentDocument) traverse(node.contentDocument);

        if (node.childNodes) node.childNodes.forEach(n => traverse(n));
    }
    traverse(document);
    return results;
    """

    elements = driver.execute_script(deep_js)

    email_btn = None

    for el in elements:
        try:
            txt = el.text.strip()
            if txt == "Email" or txt == "メール":
                email_btn = el
                break
        except:
            pass

    if not email_btn:
        raise Exception("❌ Email ボタンが見つかりません（DOM 構造が違う可能性）")

    driver.execute_script("arguments[0].click();", email_btn)
    print("✅ Email ログインボタン クリック完了！")
    time.sleep(2)
# =====================================================================
# Sign in ボタンを確実にクリックする関数
# =====================================================================
def click_signin_button():
    print("🔐 Sign in ボタンクリック開始")

    # 全ての Sign in ボタン候補を取得
    buttons = driver.find_elements(By.XPATH, "//button[contains(., 'Sign in')]")

    if not buttons:
        raise Exception("❌ Sign in ボタンが見つかりません")

    # 表示されている本物だけ残す
    visible_buttons = [btn for btn in buttons if btn.is_displayed()]

    if not visible_buttons:
        raise Exception("❌ 表示されている Sign in ボタンがありません")

    signin_btn = visible_buttons[0]

    # スクロールで確実に表示領域へ
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", signin_btn)
    time.sleep(0.5)

    # JS で強制クリック（最強）
    driver.execute_script("arguments[0].click();", signin_btn)

    print("✅ Sign in ボタンをクリックしました")
    time.sleep(3)

# =====================================================================
# 実行
# =====================================================================
login_tradingview()

for sym in SYMBOLS:
    for tf, start_date in DOWNLOAD_START_DATES.items():

        print("\n==============================")
        print(f"📌 {sym} / {tf}分足")
        print("==============================")

        driver.get(f"https://jp.tradingview.com/chart/?symbol={sym}&interval={tf}")
        time.sleep(5)

        focus_chart()
        open_jump_panel()
        select_custom_range()
        set_date_range(start_date, END_DATE)
        export_csv()

driver.quit()
print("\n🎉 全てのダウンロード完了！")
