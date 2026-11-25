/**
 * CP master sheet → AI Console (Demand dataset), every 30 minutes.
 *
 * Pushes every row of "Sheet1" to POST /api/ingest/datasets. The console creates the dataset on the first run and
 * after that updates rows by phone (new phones are added; nothing is deleted when a row leaves the sheet).
 *
 * Setup (once), in the sheet: Extensions → Apps Script → paste this file, then
 *   1. Project Settings → Script properties → add INGEST_TOKEN = the console's INGEST_TOKEN (Render env).
 *   2. Run syncCpDataset once (authorise; check the log).
 *   3. Run installTrigger once (every 30 minutes from then on).
 * A failed run throws, so Apps Script emails the trigger owner.
 */
const CONSOLE_URL = 'https://oh-ai-calling-console.onrender.com/api/ingest/datasets'; // Render directly, not Vercel
const SHEET_NAME = 'Sheet1';
const DATASET = { department: 'DEMAND', name: 'CP master (Google Sheet)' }; // renaming creates a new dataset
const CHUNK = 500; // rows per request

function syncCpDataset() {
  const token = PropertiesService.getScriptProperties().getProperty('INGEST_TOKEN');
  if (!token) throw new Error('Script property INGEST_TOKEN is not set');
  const sheet = SpreadsheetApp.getActive().getSheetByName(SHEET_NAME);
  if (!sheet) throw new Error(`No sheet named "${SHEET_NAME}"`);
  const [header, ...rows] = sheet.getDataRange().getValues();
  const col = (name) => {
    const i = header.indexOf(name);
    if (i < 0) throw new Error(`Column "${name}" is missing from ${SHEET_NAME}`);
    return i;
  };
  const iPhone = col('phone_number'), iD30 = col('d30_visits'), iAll = col('all_time_visits');
  col('name'); col('city'); // the console reads these two by name

  const count = (row, i, n) => {
    const v = row[i];
    if (v === '' || v === null) return 0;
    const x = Number(v);
    if (!Number.isFinite(x)) throw new Error(`Row ${n}: ${header[i]} is not a number: "${v}"`);
    return x;
  };

  const leads = [];
  rows.forEach((row, k) => {
    const phone = String(row[iPhone]).trim();
    if (!phone) return; // no number, nothing to call
    const lead = {};
    header.forEach((h, i) => { if (h) lead[h] = row[i] instanceof Date ? row[i].toISOString() : row[i]; });
    // Use-case doc: active = 1+ visit in the last 30 days; lapsed = visited before, not in the last 30 days.
    const d30 = count(row, iD30, k + 2), all = count(row, iAll, k + 2);
    lead.cp_status = d30 >= 1 ? 'active' : all > 0 ? 'lapsed' : 'no_visits';
    lead.phone = phone;              // the console's phone field
    lead.segment = lead.cp_status;   // the console's segment field (workflows pick active / lapsed)
    leads.push(lead);
  });
  if (!leads.length) {
    console.log('No rows with a phone_number; nothing sent');
    return;
  }

  const total = { added: 0, updated: 0, invalid: 0, duplicates: 0 };
  let datasetId = null;
  for (let i = 0; i < leads.length; i += CHUNK) {
    const r = post_(token, leads.slice(i, i + CHUNK));
    datasetId = r.dataset_id;
    Object.keys(total).forEach((key) => { total[key] += r.stats[key]; });
    if (r.errors.length) console.log(`Trigger errors: ${JSON.stringify(r.errors)}`);
  }
  console.log(`Dataset ${datasetId}: ${leads.length} rows sent · ${JSON.stringify(total)}`);
}

function post_(token, leads) {
  const res = UrlFetchApp.fetch(CONSOLE_URL, {
    method: 'post',
    contentType: 'application/json',
    headers: { Authorization: `Bearer ${token}` },
    payload: JSON.stringify({ ...DATASET, leads }),
    muteHttpExceptions: true,
  });
  const code = res.getResponseCode();
  const body = res.getContentText();
  if (code !== 200) throw new Error(`Console answered ${code}: ${body.slice(0, 500)}`);
  return JSON.parse(body);
}

/** Run once: (re)creates the 30-minute trigger for syncCpDataset. */
function installTrigger() {
  ScriptApp.getProjectTriggers()
    .filter((t) => t.getHandlerFunction() === 'syncCpDataset')
    .forEach((t) => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('syncCpDataset').timeBased().everyMinutes(30).create();
}
