# MoneyForward ME の仕様（実測）

2026-09-29 にローカルの headed Chromium で実測した。MF の画面が変わったら、この文書と `mf_me_dedup/mf.py` をあわせて直す。

## ログイン

1. `https://moneyforward.com/` を開く。
2. ボタン「ログイン / 新規登録」→ リンク「ログイン」（最初の1つ）。`https://id.moneyforward.com/sign_in` に遷移する。
3. textbox「メールアドレス」に入力 → ボタン「ログインする」。
4. textbox「パスワード」に入力 → ボタン「ログインする」。
5. textbox「認証コード（数字6桁）」に TOTP を入力 → ボタン「認証する」。
6. `https://moneyforward.com/`（URL が完全一致）に遷移したら完了。

- reCAPTCHA、確認メール、アカウント選択は出なかった（2段階認証は認証アプリ方式）。
- ログイン後のトップでは、リンク「入出金」を role で待っても見つからなかった。完了は URL で判定する。
- 1Password の TOTP 秘密鍵は `XXXX XXXX ...` のように空白入りで表示される。pyotp に渡す前に空白を除く。

## CSV の取得

```
GET https://moneyforward.com/cf/csv?from=YYYY/MM/DD&month=M&year=YYYY
```

- `from` の日付を含む MF の「月」1つ分を返す。月の境目は日によってずれる（実測では 22〜25日。営業日による調整と思われる）。
  - 例: `from=2026/08/01&month=8&year=2026` → 2026/07/24〜08/24、`from=2026/08/25&month=8&year=2026` → 2026/08/25〜09/24。
  - `from` だけでも同じ結果になる。`month`/`year` だけだと `from` を省いたときと異なる範囲になるので、必ず `from` を渡す。
  - 実装では、暦月の1日を `from` にする（その月の1日を含む MF の月が返る）。連続する暦月の1日で取ると、MF の月が隙間なくつながる。
- 本文は cp932。レスポンスの `Content-Type` は `text/csv; charset=utf-8` だが、この charset は誤り。`Content-Disposition` あり。
- ヘッダ行: `計算対象,日付,内容,金額（円）,保有金融機関,大項目,中項目,メモ,振替,ID`
  - `日付` は `YYYY/MM/DD`、`金額（円）` は符号付き整数（カンマなし）、`計算対象` / `振替` は `0` / `1`。
  - `ID` は 43文字の base64 風の文字列。画面の行 ID とは別物。
- 失敗時の応答:
  - 未ログインのとき: `302` → `https://moneyforward.com/sign_in`。リダイレクトをたどると `id.moneyforward.com/sign_in` の HTML が `200` で返る。
  - クエリなしのとき: `200` で `/cf` の HTML が返る。
  - したがって、ステータス200だけでは不十分。`Content-Type` が `text/csv` で始まることと、ヘッダ行が上記と一致することを確かめる。
- `context.request.get` で、ブラウザのログイン済みセッション（Cookie）のまま取得できる。

## 家計簿画面（`/cf`）

- `https://moneyforward.com/cf` は、現在の月を表示する。クエリ（`from` / `month` / `year`）は無視される。
- 表示中の月: `#calendar h2`（例: `2026/8/25 - 2026/9/24`。月日はゼロ埋めなし）。`.fc-header-title h2` はページ内に2つあるので使わない。
- 月の移動: `#calendar` 内の「◄」（前月）/「►」（次月）。URL は `/cf` のまま、表が非同期で入れ替わる。`#calendar h2` の変化で移動の完了を待つ。
- 1つの月の明細は、すべて1ページに表示される（実測で179行。CSV と同じ行数）。

### 行

- `tr.transaction_list#js-transaction-<数字>`。数字（以下 rid）は画面側の ID で、CSV の `ID` は DOM のどこにも入っていない。
  - rid は振替に切り替えても変わらない。
- 列:
  - 日付: `td.date` の属性 `data-table-sortable-value`。先頭10文字が `YYYY/MM/DD`（表示テキストは `MM/DD(曜)`）。
  - 内容: `td.content` のテキスト。
  - 金額: `td.amount`（クラスに `amount` を含む td）のテキストの、最初の `-?[\d,]+`。カンマ入り。
  - 保有金融機関: 口座連携の行は `td.note`、手入力の行は `td.sub_account_id_hash`。振替の行では相手先などが混ざるので、行の特定には使わない。
  - 振替の切り替え: `td` 内の `i.icon-exchange.js-switch-transfer`。
- 手入力の行は、日付・内容・金額の `td` のクラスに `form-switch-td` が付き、金額は `td.amount > .noform > span` にある。上記のセレクタのままで両方の形を読める。

### CSV の行から画面の行を特定する

`(日付, 内容, 金額)` が一致する行を探す。実測では、2026/08/25〜09/24 の179行すべてが、画面の行1つにだけ一致した（保有金融機関まで含めると、振替の行で一致しない）。

- 内容の文字の違い: CSV（cp932）の `～`（U+FF5E FULLWIDTH TILDE）が、画面では `〜`（U+301C WAVE DASH）になる。比べる前に画面側の U+301C を U+FF5E に置き換える。
  - 実測で見つかったのはこの1種類だけ。cp932 の既知の違い（`‖`/`∥`、`−`/`－`、`¢`/`￠`、`£`/`￡`、`¬`/`￢`、`—`/`―`）もあわせて置き換えてよい。
- 一致する行が0件または2件以上なら、変更せずに例外にする。

### 振替の状態

| 状態 | `i.icon-exchange` のクラス | `tr` のクラス | `.transfer_account_box` | `data-link` |
|---|---|---|---|---|
| 通常 | `onchange` なし | `mf-grayout` なし | 空 | `/cf/update.js?change_type=enable_transfer&id=<rid>` |
| 振替（相手先あり） | `onchange` あり | `mf-grayout` あり | 相手先の口座名（例: `楽天市場(my Rakuten)`） | `...change_type=disable_transfer...` |
| 振替（相手先なし） | `onchange` あり | `mf-grayout` あり | 空。行内に `a#change_act_type_<rid>` がある | `...change_type=disable_transfer...` |

CSV との対応（179行）: `振替`=1 の31行はすべて `onchange` あり、`振替`=0 の148行はすべて `onchange` なし。

### 振替への変更

1. 行の `td:nth-child(9) > .icon-exchange` をクリック → リンク「実行する」をクリック。行が振替（相手先なし）になる。
2. `#change_act_type_<rid>`（`href="#modal_change_act_type"` のリンク）をクリックし、モーダル `#modal_change_act_type` を開く。
3. `#user_asset_act_partner_account_id_hash` で口座を選ぶ。option の value はハッシュなので、ラベルで選ぶ（`楽天市場(my Rakuten)` はちょうど1つ）。
4. 口座を選ぶと `#user_asset_act_partner_sub_account_id_hash` が現れる。サブ口座をラベルで選ぶ。
   - 楽天市場(my Rakuten) のサブ口座は5つ。使うのは `楽天市場`（先頭の既定値ではない）。
   - option のテキストは前後に空白を含むので、空白を除いて比べ、一致する option の value を `select_option` に渡す。
5. ボタン「設定を保存」をクリック。
6. 確認: 同じ rid の行で、`.icon-exchange` に `onchange` があり、`.transfer_account_box` のテキストに相手先の口座名が含まれること。

### 振替を元に戻す

振替の行の `.icon-exchange` をクリック → リンク「実行する」。通常の支出に戻る（`data-link` が `disable_transfer` のもの）。

## 反映の確認（CSV の再取得）

振替の前後で CSV（2026/08/25〜09/24、179行）を比べた結果:

- 対象の明細は、`振替` が 0→1、`計算対象` が 1→0 になった。
- `ID` は変わらない（前後の ID の集合が一致。追加・消滅なし）。
- 対象以外の行（市場側を含む）は、どの列も変わらない。対になる行も作られない。

`__main__.py` の前提（振替後も `ID` が変わらない、`振替` = `"1"` で判定できる）と一致する。
