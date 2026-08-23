# Costpoint Mobile T&E — `serverMethod=api` Protocol (verified)

All values below are extracted **verbatim** from `captured/app.js`
(`DeltekCostpointTE.common.ApiRequestBuilder`) and verified with
`06_verify_protocol.py` — not paraphrased. This is Costpoint's application
**result-set (RS)** protocol, tunneled through `jsonproxy.php`.

## Transport

Every data operation is a POST to `…/cpshared/backend/jsonproxy.php` with
form fields:
```
requestType  = POST
serverMethod = api
payload      = <apiRequestJson>     # the JSON string built below
cookieData   = <session cookies from login>
```

## Batch envelope — `createMainRequestJson(requestObjList)`

One or more request objects are wrapped into a single payload:
```json
{
  "requests": [ <requestObj>, <requestObj>, ... ],
  "ProcIdSeed": "<DeltekCp.common.Settings.ProcIdSeed>"
}
```
`ProcIdSeed` is a per-session value seeded at login (see client). Each
`<requestObj>` is exactly one of the builder outputs below.

## Attribute → wire-key map (statics block, verbatim)

```
procIdSeedAttr 'ProcIdSeed'   appIdAttr 'appId'        parentRsIdAttr 'parentRSId'
openAppAttr 'openApp'         rsIdAttr 'rsId'           parentCtxTreeAttr 'parentCtxTree'
closeAppAttr 'closeApp'       rsDataAttr 'rsData'       rowNoAttr 'rowNo'
openRsAttr 'openRS'           rowFilterAttr 'rowFilter' sortAttr 'sort'
getRSDataAttr 'getRSData'     rowRangeAttr 'rowRange'   columnRangeAttr 'columnRange'
getRSMetadataAttr 'getRSMetadata'  queryRSDataAttr 'queryRSData'  whereAttr 'where'
putRSDataAttr 'putRSData'     objectIdAttr 'objectId'   lookupObjectIdAttr 'lookupObjectId'
saveAppAttr 'saveApp'         warningsOkAttr 'warningsOk'  runActionAttr 'runAction'
validateRowAttr 'validateRow' validateFieldAttr 'validateField'  actionIdAttr 'actionId'
wizardModeAttr 'wizardMode'   childNoAttr 'childNo'
```
Note the capitalization quirks: `parentRSId`, `openRS` (RS uppercase).

## Request builders (each returns ONE requestObj)

```jsonc
// openApp(appId, wizardMode?)
{"openApp": {"appId": "<APP>", "wizardMode": <opt>}}

// closeApp(appId)
{"closeApp": {"appId": "<APP>"}}

// openRs(appId, parentRsId, rsId, lookupObject?, childNo?)
{"openRS": {"appId": "<APP>", "parentRSId": "<PARENT>", "rsId": "<RS>",
            "lookupObjectId": <opt>, "childNo": <opt>}}

// getRSData(appId, parentRsId, rsId, parentCtxTree, rowStart?, rowEnd?, cols?, rowFilter?, lookup?)
{"getRSData": {"appId": "<APP>", "parentRSId": "<PARENT>", "rsId": "<RS>",
               "parentCtxTree": "<CTX>", "rowRange": {"start": <n>, "end": <n>}}}

// putRSData(appId, parentRsId, rsId, parentCtxTree, rsDataList)
{"putRSData": {"appId": "<APP>", "parentRSId": "<PARENT>", "rsId": "<RS>",
               "parentCtxTree": "<CTX>", "rsData": [ <rowObj>, ... ]}}

// saveApp(appId, warningsOk?)
{"saveApp": {"appId": "<APP>", "warningsOk": "1"}}

// runAction(appId, actionId, parentRsId?, rsId?, parentCtxTree?, rowNo?, lookup?)
{"runAction": {"appId": "<APP>", "actionId": "<ACTION>", ...}}
```

## Row object shape — `model.buildPutRsObject()`

Each RS row written via `putRSData.rsData[]`:
```json
{
  "rowNo": <ROW_NO>,
  "status": ["updated"],          // 'M'→updated, 'N'→new, 'D'→deleted, 'S'→selected
  "data": [ {"FIELD_NAME": value}, {"OTHER_FIELD": value}, ... ]
}
```
`data` is a **list of single-key objects**, one per persisted model field.
`Date` values are passed through `Common.convertDateToRequestFormat`.

## Timesheet app constants

| Thing | Value |
|---|---|
| App id (own timesheet) | `TMMTIMESHEET` |
| App id (proxy/approve)  | `TMMTIMESHEET_APPROVE` |
| Header result set       | `TMMTS` |
| Line result set         | `TMMTS_TS_LINE` |
| Charge favorites RS      | `TMMTS_CHARGE_FAVE` (charge lookup model: `ChargeLookup`) |
| Context (own / proxy)   | `'.'` / `'.0'` ; line ctx `'.'+headerRowNo` / `'.0.'+headerRowNo` |

Daily hours fields on a line: `DAY1_HRS` … `DAY{n}_HRS` (confirmed `DAYn_HRS`
naming; a weekly period uses DAY1_HRS..DAY7_HRS, Mon→index per period start).
Line total: `TOTAL_ENTERED_HRS`. Charge identity on a line/lookup:
`CHARGE_TREE_CD`, `CHARGE_BRANCH_CD`, `CHARGE_CD`, `UDT01_ABBRV_ID`…`UDT15_ABBRV_ID`.

## Save (draft) — exact request list

From `getSaveTimesheetLineData()` + `getSaveTimesheetData(false)` + `saveApp`:
```json
{
  "requests": [
    {"putRSData": {"appId":"TMMTIMESHEET","parentRSId":"TMMTS","rsId":"TMMTS_TS_LINE",
                   "parentCtxTree":".<headerRowNo>",
                   "rsData":[ {"rowNo":<n>,"status":["updated"],
                              "data":[ /* changed line fields incl DAYx_HRS */ ]} ]}},
    {"putRSData": {"appId":"TMMTIMESHEET","parentRSId":"","rsId":"TMMTS",
                   "parentCtxTree":".",
                   "rsData":[ {"rowNo":<headerRowNo>,"status":["updated"],
                              "data":[ {"ACTION_CD":""}, {"S_STATUS_CD":"<cur>"} ]} ]}},
    {"saveApp": {"appId":"TMMTIMESHEET","warningsOk":"1"}}
  ],
  "ProcIdSeed": "<seed>"
}
```

## Save + SIGN — difference

`getSaveTimesheetData(true)` sets the header row `data` to `[{"ACTION_CD":"S"}]`
(instead of `ACTION_CD:""`+`S_STATUS_CD`). Approve (proxy) uses `ACTION_CD:"A"`
and `status:["updated","selected"]` (`getApproveTimesheetPutRSData`).

## Load sequence — VERIFIED LIVE

Response envelope for `serverMethod=api`:
```json
{"responses": [ {"<method>": {"respCode": 0, "metaData": {...}, "rsData": [...]}}, ... ]}
```
`getRSData`/`queryRSData` rows look like `{"rowNo": <n>, "data": [{"FIELD": val}, ...]}`
(the same single-key-list shape as writes). `queryRSData` also returns `rowCount`.

**Header (establishes the period context):**
```
openApp("TMMTIMESHEET")
openRS("TMMTIMESHEET","","TMMTS")
queryRSData("TMMTIMESHEET","","TMMTS",".", where=[[ {TS_SCHEDULE_CD=}, {YEAR_NO_CD=}, {PERIOD_NO_CD=} ]])
getRSData("TMMTIMESHEET","","TMMTS",".")
```
WHERE conditions are `{"objectId","operator":"=","value"}`; the list is wrapped
once more (`where: [ [cond, cond, ...] ]`). For a non-current period the app
filters on `END_DT` instead. The header row gives `rowNo` (→ line context),
`S_STATUS_CD`, `END_DT`, `TS_SCHEDULE_CD`/`YEAR_NO_CD`/`PERIOD_NO_CD`, and
`DAYn_LABEL` strings like `"Fri<BR>06/05/26"` → the **DAYn ↔ calendar-date map**.

**Lines (child of the header row):**
```
openRS("TMMTIMESHEET","TMMTS","TMMTS_TS_LINE")
getRSMetadata("TMMTIMESHEET","TMMTS","TMMTS_TS_LINE", ".<headerRowNo>")
queryRSData("TMMTIMESHEET","TMMTS","TMMTS_TS_LINE", ".<headerRowNo>",
            sort=[{objectId:"LINE_NO",order:"asc"}], where=[[ same 3 conds ]])
getRSData("TMMTIMESHEET","TMMTS","TMMTS_TS_LINE", ".<headerRowNo>")
```
Skipping the `queryRSData` step yields **0 line rows** — the query is what makes
the child rows readable. Line ctx is `.<headerRowNo>` (own) / `.0.<headerRowNo>` (proxy).

### Verified line fields (own timesheet, this tenant)
A line row carries (66 fields total): `rowNo`, `LINE_NO`, `LINE_DESC`,
`UDT02_ID`/`UDT02_NAME` (Project, e.g. `01234.00A` / "Project Work"),
`UDT01_ID`/`UDT01_NAME` (Account, `0800-000` / "G&A LABOR"),
`UDT09_ID` (Org `1.01.01`), `UDT10_ID`/`UDT10_NAME` (Paytype `R`/"Regular"),
`DAY1_HRS…DAYn_HRS` (string, e.g. `"8"`), `TOTAL_ENTERED_HRS`, `HRS_MODIFY_FL`,
`PERIOD_NO_CD`, `YEAR_NO_CD`, `TS_SCHEDULE_CD`, `S_STATUS_CD`. To fill a day:
`putRSData` a row `{rowNo:<line rowNo>, status:["updated"], data:[{DAYk_HRS:"8"}]}`.

This tenant runs a **semi-monthly (HDS)** schedule: periods are the 1st–15th and
16th–EOM, so `DAY1…DAY15`/`DAY16…` — NOT a Mon–Sun week. Always resolve the day
index from the header's `DAYn_LABEL` dates, never from `weekday()`.

## Adding a new charge line (PTO / Holiday) — VERIFIED LIVE

Charge pick-list (`TMMTS_CHARGE_FAVE`) load, child of the header row:
```
openRS("TMMTIMESHEET","TMMTS","TMMTS_CHARGE_FAVE")
queryRSData(... ".0", sort=[{SEQ_NO desc}], where=[[ {objectId:"DFLT_UDT02_ID",operator:"is not null",value:""} ]])
getRSData(... ".0")
```
Each favorite carries the value that matters (`DFLT_UDT02_ID`) plus flags that
say what kind of charge it is, which is how setup assigns them without asking:

| Field | Meaning |
|-------|---------|
| `DFLT_UDT02_ID` | project id to set on a new line |
| `CHARGE_DESC`   | human label (e.g. `LEAVE - HOLIDAY`) |
| `HOLIDAY_FL`    | `Y` on the holiday charge |
| `VACATION_FL`   | `Y` on the PTO / vacation charge |
| `CHARGE_CD`, `DFLT_UDT01_ID`, `DFLT_UDT10_ID` | resolved charge, account, paytype |

Add-line flow (uncommitted until `saveApp`; new-line sentinel rowNo `-59999`):
```
# 1. create empty new line  (the TS_LINE result set must already be open/loaded)
putRSData("TMMTIMESHEET","TMMTS","TMMTS_TS_LINE", ".<hdrRowNo>",
          [{rowNo:-59999, status:["new"], data:[]}])
runAction("TMMTIMESHEET","TMMTS_NEW_TS_LINE","TMMTS","TMMTS_TS_LINE", ".<hdrRowNo>", rowNo:-59999)
getRSData(... -59999..-59998)
# 2. set the charge, let the server resolve it
putRSData(... [{rowNo:-59999, status:["updated"], data:[{UDT02_ID:"<DFLT_UDT02_ID>"},{ADD_FAVORITES_FL:"Y"}]}])
validateField("TMMTIMESHEET","TMMTS","TMMTS_TS_LINE", ".<hdrRowNo>", -59999, objectId:"UDT02_ID")
getRSData(... -59999..-59998)   # now carries LINE_DESC + CHARGE_*/UDT01/UDT09/UDT10
# 3. fill hours + commit  (status stays "new" on the line putRSData)
putRSData(... [{rowNo:-59999, status:["new"], data:[{UDT02_ID:..},{ADD_FAVORITES_FL:"Y"},{DAYk_HRS:"8"}]}])
putRSData(... TMMTS header row, ACTION_CD:"") ; saveApp("TMMTIMESHEET","1")
```
Gotcha: a `putRSData` to `TMMTS_TS_LINE` before the line result-set has been
opened/loaded fails with *"Result set TMMTS_TS_LINE not found for parent TMMTS"* —
always run the period+line load first.

This is wired in `timesheet.py`: `--charge holiday|pto` adds the line if absent
(via `TimesheetAutomation.ensure_line`/`_stage_new_line`) then fills the day.
