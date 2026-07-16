# ADR-001: 使用 HTML5 Dataset 進行前端資料綁定

**狀態**: 已採納
**日期**: 2026-07-17
**決策者**: AI / 用戶

---

## 背景

在修改知識庫條目時，原先的做法是透過 Bootstrap Modal 的 `onclick` 事件，直接將大段落的文字內容當作參數傳入 JavaScript 函數，或者是嘗試用 JSON 解析。然而，知識庫的內容經常包含換行符號、引號、特殊字元，這些字元會直接破壞 HTML 的結構，導致 `SyntaxError: missing ) after argument list` 或是點擊修改按鈕完全沒有反應（對話框沒有帶入資料）。

## 決策

全面改用 HTML5 原生的 `data-*` 屬性（Dataset）來進行資料綁定與傳遞。在後端算圖時，利用 `jinja` 的 `| e` (escape) 將文字安全地放入 `data-content` 等屬性中。

## 考慮的選項

### 選項 A: JavaScript 字串替換與 JSON 解析

- ✅ 優點：不需改變既有 HTML 結構
- ❌ 缺點：對於不可預測的使用者輸入（單引號、雙引號混合換行），非常容易在渲染時就報錯，防禦性極差。

### 選項 B: HTML5 Dataset 綁定

- ✅ 優點：原生支援，瀏覽器會自動處理屬性內的跳脫字元；在 JS 端只需透過 `button.getAttribute('data-content')` 就能安全且完整地取出原本的長文。
- ❌ 缺點：稍微增加 HTML 的大小，但對於後台管理介面來說微不足道。

## 理由

為了確保系統的強健性（Robustness）與防禦性編程（Defensive Coding），前端與後端之間的資料傳遞必須能容忍任何形式的特殊字元，Dataset 是目前最穩定且標準的做法。

## 影響

### 正面影響

- 徹底解決了「修改按鈕點擊無效」、「資料無法帶入對話框」的 Bug。
- 降低了未來發生 XSS 或語法錯誤的風險。

## 相關連結

- `/home/pipadmin/文件/admin/templates/knowledge.html`
