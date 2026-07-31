"""
core.palette — 指標線色盤 (ADR-138)。

原本 `StockTradingAppPro.color_map` 只有寫死的 8 色,MA1~MA6 就吃掉 6 色,
再加布林兩組、黃金切割、副圖指標,顏色根本不夠分,兩條線常常同色疊在一起
看不出誰是誰。使用者要求「所有指標線的顏色可以更多選擇。最好有 255 種」。

## 設計

- **舊有的 8 色原封不動排在最前面**,而且**標籤字串一字未改**
  (`"黃 (#FFCA28)"` …)。這點是強制的:`indicator_settings.json` 存的是
  **標籤字串**不是色碼,標籤一改,使用者存過的顏色就會全部對不上而退回
  預設色 —— 那是靜默的資料遺失 (P-67 同類:同一份資訊兩處各自維護)。
- 後面接 **255 色**的系統色盤:20 個色相 × 12 個明暗/彩度階 + 15 階灰。
  色相之間固定間隔 18°,同色相的 12 階刻意混合「不同明度」與「不同彩度」,
  因為主圖背景是深色 (#131722 一類),純粹只調明度的話低明度那幾階會整片
  糊在背景裡看不見。
- `resolve()` 對「標籤不在色盤裡」是**容錯**的:標籤裡本來就內嵌 `#RRGGBB`,
  直接從字串解析回來。這樣即使未來色盤再調整,舊設定檔也不會突然變色。

零 tkinter、零 shioaji 依賴 (鐵則 11),可離線測試。
"""
import colorsys
import re

# ---------------------------------------------------------------------------
# 舊有 8 色 —— 標籤字串不可更動 (見模組 docstring)
# ---------------------------------------------------------------------------
LEGACY_COLORS = (
    ("黃 (#FFCA28)", "#FFCA28"),
    ("白 (#FFFFFF)", "#FFFFFF"),
    ("藍 (#29B6F6)", "#29B6F6"),
    ("紫 (#E040FB)", "#E040FB"),
    ("橘 (#FF9100)", "#FF9100"),
    ("紅 (#FF1744)", "#FF1744"),
    ("青 (#00E5FF)", "#00E5FF"),
    ("綠 (#00E676)", "#00E676"),
)

# 20 個色相 (名稱, 角度)。間隔 18°,涵蓋整圈。
HUES = (
    ("紅", 0), ("朱紅", 18), ("橙", 36), ("琥珀", 54), ("黃", 72),
    ("黃綠", 90), ("萊姆", 108), ("草綠", 126), ("翠綠", 144), ("青綠", 162),
    ("青", 180), ("天青", 198), ("湛藍", 216), ("藍", 234), ("靛", 252),
    ("紫", 270), ("蘭紫", 288), ("洋紅", 306), ("桃紅", 324), ("玫瑰", 342),
)

# 同一色相的 12 階 (彩度, 明度)。深色背景下明度低於 0.45 幾乎看不見,
# 所以最暗只到 0.45,剩下的區隔靠彩度拉開。
LEVELS = (
    (1.00, 0.45), (1.00, 0.60), (1.00, 0.75), (1.00, 0.88), (1.00, 1.00),
    (0.78, 0.60), (0.78, 0.80), (0.78, 1.00),
    (0.55, 0.72), (0.55, 0.92),
    (0.36, 0.85), (0.36, 1.00),
)

GRAY_STEPS = 15   # 20 * 12 + 15 = 255


def _hex_of(h_deg, s, v) -> str:
    r, g, b = colorsys.hsv_to_rgb((h_deg % 360) / 360.0, s, v)
    return "#{:02X}{:02X}{:02X}".format(
        int(round(r * 255)), int(round(g * 255)), int(round(b * 255)))


def _build_generated():
    out = []
    for name, deg in HUES:
        for i, (s, v) in enumerate(LEVELS, start=1):
            out.append((f"{name}{i:02d} ({_hex_of(deg, s, v)})", _hex_of(deg, s, v)))
    for i in range(GRAY_STEPS):
        # 從 #202020 到 #F0F0F0 均分,兩端都不取到純黑/純白 ——
        # 純黑在深色背景等於隱形,純白已經在舊有 8 色裡了。
        lv = int(round(0x20 + (0xF0 - 0x20) * i / (GRAY_STEPS - 1)))
        hx = "#{0:02X}{0:02X}{0:02X}".format(lv)
        out.append((f"灰{i + 1:02d} ({hx})", hx))
    return tuple(out)


GENERATED_COLORS = _build_generated()

#: 完整色盤 = 舊有 8 色 (排最前面,使用者原本挑的都還在原位) + 255 系統色
PALETTE = LEGACY_COLORS + GENERATED_COLORS

_HEX_RE = re.compile(r'#([0-9A-Fa-f]{6})')


def color_map() -> dict:
    """回傳 {標籤: 色碼};dict 保持插入順序,直接拿 keys() 當下拉選單的值。"""
    return dict(PALETTE)


def labels() -> list:
    return [lb for lb, _ in PALETTE]


def resolve(label, default="#FFFFFF") -> str:
    """標籤 → 色碼。找不到就從標籤內嵌的 `#RRGGBB` 解析;再不行才回 default。

    容錯是刻意的:設定檔存的是標籤字串,色盤日後若再調整,舊標籤仍然要能
    還原成當初那個顏色,不可以靜默變色。
    """
    if not label:
        return default
    hit = dict(PALETTE).get(label)
    if hit:
        return hit
    m = _HEX_RE.search(str(label))
    if m:
        return "#" + m.group(1).upper()
    return default
