"""
brokers — 多券商 adapter 套件 (ADR-097 階段 0)。

未來規劃：永豐 (shioaji)、群益、兆豐、凱基四家券商各自一個 adapter 模組，
對外提供跟券商無關的統一介面 (base.BrokerClient)，讓 stock_app_pro.py
可以「同時登入多家、依帳戶分流下單」，不必為每家券商各寫一套呼叫邏輯。

跟 core/ 、data/ 不同，這個套件允許依賴各券商的第三方 SDK (shioaji 等)，
因為它存在的理由是「封裝券商 SDK 差異」而不是「離線可測的純邏輯」。
"""
