#!/usr/bin/env node
/**
 * FTO Dashboard — サーバーサイド・データ取得スクリプト（GitHub Actions専用）
 *
 * 背景（R-02対応）: これまでブラウザから corsproxy.io 経由で FRED API を叩いていたため、
 * (a) corsproxy.io の仕様変更で全系列 HTTP 403、(b) FRED APIキーが公開HTMLに平文で
 * 埋め込まれる、という二重の問題があった。本スクリプトは GitHub Actions（サーバー環境）
 * から直接 FRED / GDELT を叩き、結果を data/latest.json に書き出す。
 * ブラウザ側（index.html）はこの静的JSONを同一オリジンで読むだけになり、
 * (a) CORSプロキシ依存が消え、(b) APIキーがクライアントに一切露出しなくなる。
 *
 * 取得失敗時の方針（R-01の思想を踏襲）: 前回値で埋めない。status:"failed" のまま
 * 正直に残す。取得できなかったことを「取得できた値」に見せかけない。
 */

const fs = require('fs');
const path = require('path');

const FRED_API_KEY = process.env.FRED_API_KEY;
if (!FRED_API_KEY) {
  console.error('FATAL: 環境変数 FRED_API_KEY が設定されていません（GitHub Secretsを確認）。');
  process.exit(1);
}

const FRED_KEYS = ['DEXJPUS','CPIAUCSL','CPILFESL','CUSR0000SEFV','UNRATE','CES0500000003','FEDFUNDS','DGS10','UMCSENT'];

const GDELT_TOPICS = [
  { id: "taiwan_strait", label: "台湾海峡・中国軍事圧力", role: "eastAsia", weight: 1.25,
    query: "(Taiwan OR \"Taiwan Strait\") (China OR PLA OR military OR blockade OR drills)" },
  { id: "north_korea", label: "北朝鮮ミサイル・核", role: "eastAsia", weight: 1.15,
    query: "(\"North Korea\" OR DPRK) (missile OR nuclear OR launch OR military OR sanctions)" },
  { id: "indopacific", label: "米インド太平洋・前方展開", role: "forwardPosture", weight: 1.20,
    query: "(USINDOPACOM OR \"Indo-Pacific\" OR Pentagon) (China OR Taiwan OR Japan OR deployment OR exercise)" },
  { id: "usfj_okinawa", label: "USFJ・沖縄基地・嘉手納", role: "okinawaBase", weight: 1.35,
    query: "(USFJ OR \"U.S. Forces Japan\" OR Kadena OR Okinawa OR Futenma) (base OR military OR Marines OR relocation)" },
  { id: "henoko", label: "辺野古・基地移設", role: "okinawaBase", weight: 1.00,
    query: "(Henoko OR Futenma OR Okinawa) (relocation OR base OR protest OR landfill)" },
  { id: "us_defense_budget", label: "米国防予算・国防産業", role: "defenseBudget", weight: 1.20,
    query: "(\"defense budget\" OR Pentagon OR \"Department of Defense\" OR \"military spending\")" },
  { id: "trump_defense", label: "トランプ政権・国防政策", role: "usPolitics", weight: 1.15,
    query: "(Trump OR Hegseth OR \"White House\") (\"defense strategy\" OR Pentagon OR China OR Taiwan)" },
  { id: "us_economy_stress", label: "米国経済・消費ストレス", role: "consumerStress", weight: 1.00,
    query: "(\"U.S. economy\" OR inflation OR layoffs OR \"consumer sentiment\" OR \"job market\")" }
];

const RISK_WORDS = ['missile','nuclear','war','attack','drill','exercise','deployment','blockade',
  'invasion','sanction','tariff','inflation','layoff','shutdown','protest','incident','warning',
  'crisis','tension','military','budget','Taiwan','China','PLA','Indo-Pacific'];
const POS_WORDS = ['agreement','dialogue','cooperation','stable','growth','cooling','decline',
  'easing','peace','support','alliance','deterrence','investment','readiness'];

const GDELT_TIMESPAN = '30d';
const FRED_HISTORY_LIMIT = 30; // 直近30オブザベーション（チャート・YoY計算用）
const FETCH_TIMEOUT_MS = 15000;

function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

function nowJstIso() {
  const now = new Date();
  const jst = new Date(now.getTime() + 9 * 60 * 60 * 1000);
  return jst.toISOString().replace('Z', '+09:00');
}
function jstDateStr() {
  const now = new Date();
  const jst = new Date(now.getTime() + 9 * 60 * 60 * 1000);
  return jst.toISOString().slice(0, 10);
}

async function fetchWithTimeout(url, ms) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), ms);
  try {
    const r = await fetch(url, { signal: ctrl.signal });
    return r;
  } finally {
    clearTimeout(t);
  }
}

async function fetchFredSeries(seriesId) {
  const url = `https://api.stlouisfed.org/fred/series/observations?series_id=${seriesId}&api_key=${FRED_API_KEY}&limit=${FRED_HISTORY_LIMIT}&sort_order=desc&file_type=json`;
  try {
    const r = await fetchWithTimeout(url, FETCH_TIMEOUT_MS);
    if (!r.ok) {
      return { status: 'failed', value: null, date: null, error: `HTTP ${r.status}`, history: [] };
    }
    const j = await r.json();
    const obs = (j.observations || []).filter(o => o.value !== '.' && o.value !== '');
    if (obs.length === 0) {
      return { status: 'failed', value: null, date: null, error: 'no observations returned', history: [] };
    }
    const history = obs.map(o => ({ date: o.date, value: parseFloat(o.value) }))
                        .sort((a, b) => (a.date > b.date ? 1 : -1));
    const latest = history[history.length - 1];
    return { status: 'ok', value: latest.value, date: latest.date, error: null, history };
  } catch (e) {
    return { status: 'failed', value: null, date: null, error: (e && e.message) || 'fetch failed', history: [] };
  }
}

function scoreArticles(articles, weight) {
  if (articles === null) return null;
  if (articles.length === 0) return 18;
  const text = articles.map(a => (a.title || '') + (a.seendate || '')).join(' ').toLowerCase();
  const riskHits = RISK_WORDS.filter(w => text.includes(w.toLowerCase())).length;
  const posHits = POS_WORDS.filter(w => text.includes(w.toLowerCase())).length;
  const raw = 18 + Math.log(1 + articles.length) * 18 + riskHits * 1.9 - posHits * 0.9;
  return clamp(Math.round(raw * weight), 0, 100);
}

async function fetchGdeltTopic(topic) {
  const q = encodeURIComponent(topic.query);
  const url = `https://api.gdeltproject.org/api/v2/doc/doc?query=${q}&mode=artlist&format=json&maxrecords=50&sort=datedesc&timespan=${GDELT_TIMESPAN}&sourcelang=english`;
  try {
    const r = await fetchWithTimeout(url, FETCH_TIMEOUT_MS);
    if (!r.ok) {
      return { status: 'failed', value: null, date: null, error: `HTTP ${r.status}`, article_count: 0, articles: [] };
    }
    const j = await r.json();
    const articles = j.articles || [];
    const score = scoreArticles(articles, topic.weight);
    const top = articles.slice(0, 10).map(a => ({
      title: a.title || '', url: a.url || '', domain: a.domain || '', seendate: a.seendate || ''
    }));
    return { status: 'ok', value: score, date: jstDateStr(), error: null, article_count: articles.length, articles: top };
  } catch (e) {
    return { status: 'failed', value: null, date: null, error: (e && e.message) || 'timeout/failed', article_count: 0, articles: [] };
  }
}

async function main() {
  console.log('=== FTO Dashboard データ取得開始 ===');
  console.log('FRED系列:', FRED_KEYS.length, '/ GDELTシグナル:', GDELT_TOPICS.length);

  const fredResults = await Promise.allSettled(FRED_KEYS.map(k => fetchFredSeries(k)));
  const fred = {};
  let fredOk = 0;
  FRED_KEYS.forEach((k, i) => {
    const r = fredResults[i];
    fred[k] = r.status === 'fulfilled' ? r.value
      : { status: 'failed', value: null, date: null, error: (r.reason && r.reason.message) || 'unknown error', history: [] };
    if (fred[k].status === 'ok') fredOk++;
    console.log(`  FRED ${k}: ${fred[k].status}${fred[k].status === 'failed' ? ' (' + fred[k].error + ')' : ' value=' + fred[k].value}`);
  });

  const gdeltResults = await Promise.allSettled(GDELT_TOPICS.map(t => fetchGdeltTopic(t)));
  const gdelt = {};
  let gdeltOk = 0;
  GDELT_TOPICS.forEach((t, i) => {
    const r = gdeltResults[i];
    gdelt[t.id] = r.status === 'fulfilled' ? r.value
      : { status: 'failed', value: null, date: null, error: (r.reason && r.reason.message) || 'unknown error', article_count: 0, articles: [] };
    if (gdelt[t.id].status === 'ok') gdeltOk++;
    console.log(`  GDELT ${t.id}: ${gdelt[t.id].status}${gdelt[t.id].status === 'failed' ? ' (' + gdelt[t.id].error + ')' : ' score=' + gdelt[t.id].value}`);
  });

  const output = {
    fetched_at: nowJstIso(),
    fred_ok_count: fredOk,
    fred_total: FRED_KEYS.length,
    gdelt_ok_count: gdeltOk,
    gdelt_total: GDELT_TOPICS.length,
    fred,
    gdelt,
  };

  const outDir = path.join(__dirname, '..', 'data');
  const historyDir = path.join(outDir, 'history');
  fs.mkdirSync(historyDir, { recursive: true });

  const latestPath = path.join(outDir, 'latest.json');
  fs.writeFileSync(latestPath, JSON.stringify(output, null, 2) + '\n');
  console.log('書き出し完了:', latestPath);

  const historyPath = path.join(historyDir, `${jstDateStr()}.json`);
  fs.writeFileSync(historyPath, JSON.stringify(output, null, 2) + '\n');
  console.log('書き出し完了:', historyPath);

  console.log(`=== 完了: FRED ${fredOk}/${FRED_KEYS.length}, GDELT ${gdeltOk}/${GDELT_TOPICS.length} ===`);

  // FREDが全滅の場合は非ゼロ終了させ、Actions側で異常を検知できるようにする
  // （ワークフロー自体は失敗させても、直前のlatest.jsonは正直な失敗記録として
  //   すでにコミット対象になっているので、事業判断側が困ることはない）
  if (fredOk === 0 && gdeltOk === 0) {
    console.error('WARNING: FRED・GDELTともに全滅。ネットワーク/APIキー/上流サービスを確認してください。');
  }
}

main().catch(e => {
  console.error('FATAL:', e);
  process.exit(1);
});
