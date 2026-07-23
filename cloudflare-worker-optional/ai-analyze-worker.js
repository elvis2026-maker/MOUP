/**
 * 艾維斯玩股所 —「個股分析」後端
 * 部署為 Cloudflare Worker，網址：https://elvis-moup-api.elvis-liu2027.workers.dev
 *
 * 前端（index.html 的 AI_ENDPOINT）會 POST 以下格式過來：
 *   { "query": "2377", "stock": { ...stocks.json 裡該檔股票的完整物件, 或 null } }
 *
 * 這支 Worker 會呼叫 Google Gemini API，並帶有：
 *   - 3 組 API Key 備援（額度用完或被限流會自動換下一組）
 *   - 2 個模型備援：主要用 gemini-3.5-flash，3 組 Key 都失敗時改用
 *     gemini-3.1-flash-lite 再試一輪
 * 全部都失敗才回傳 500，前端會自動退回規則式分析，使用者不會看到錯誤畫面。
 *
 * ── 部署步驟 ──
 * 1. Cloudflare Dashboard → Workers 和 Pages → 建立 Worker，名稱 elvis-moup-api
 *    （網域會自動變成 elvis-moup-api.你的帳號.workers.dev）
 * 2. 貼上這整份程式碼，部署
 * 3. Worker → 設定 → 變數與機密，新增 3 個「機密 (Secret)」：
 *      MO_GEMINI_API_KEY  = 第一組 Gemini API Key
 *      MO2_GEMINI_API_KEY = 第二組 Gemini API Key
 *      MO3_GEMINI_API_KEY = 第三組 Gemini API Key
 *    （Gemini API Key 申請位置：https://aistudio.google.com/apikey）
 * 4. index.html 裡的 AI_ENDPOINT 已經指到這支 Worker 的網址，不用再改
 */

const MODELS = ["gemini-3.5-flash", "gemini-3.1-flash-lite"];
const GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models";

export default {
  async fetch(request, env) {
    const corsHeaders = {
      "Access-Control-Allow-Origin": "*", // 正式上線建議改成 https://elvis2026-maker.github.io
      "Access-Control-Allow-Methods": "POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
    };

    if (request.method === "OPTIONS") {
      return new Response(null, { headers: corsHeaders });
    }
    if (request.method !== "POST") {
      return new Response("Method Not Allowed", { status: 405, headers: corsHeaders });
    }

    let query = "", stock = null;
    try {
      const body = await request.json();
      query = body.query || "";
      stock = body.stock || null;
    } catch (e) {
      return new Response(JSON.stringify({ error: "無法解析請求內容" }), {
        status: 400,
        headers: { "Content-Type": "application/json", ...corsHeaders },
      });
    }

    // 3 組備援金鑰，依序輪替
    const apiKeys = [
      env.MO_GEMINI_API_KEY,
      env.MO2_GEMINI_API_KEY,
      env.MO3_GEMINI_API_KEY,
    ].filter(Boolean);

    if (apiKeys.length === 0) {
      return new Response(JSON.stringify({ error: "尚未設定任何 Gemini API Key" }), {
        status: 500,
        headers: { "Content-Type": "application/json", ...corsHeaders },
      });
    }

    const prompt = buildPrompt(query, stock);

    let lastError = "";
    // 先跑主模型的 3 組 Key，全部失敗才換下一個模型再跑一輪
    for (const model of MODELS) {
      for (const apiKey of apiKeys) {
        try {
          const result = await callGemini(model, apiKey, prompt);
          if (result) {
            return new Response(JSON.stringify({
              name: stock?.name,
              sid: stock?.sid,
              probability: result.probability,
              summary: result.summary,
              reasons: result.reasons || [],
              _model: model, // 除錯用，前端不會顯示
            }), {
              headers: { "Content-Type": "application/json", ...corsHeaders },
            });
          }
        } catch (err) {
          lastError = `${model}: ${err.message}`;
          // 這組 Key／模型失敗，繼續試下一組
        }
      }
    }

    return new Response(JSON.stringify({ error: `全部金鑰／模型皆失敗：${lastError}` }), {
      status: 500,
      headers: { "Content-Type": "application/json", ...corsHeaders },
    });
  },
};

function buildPrompt(query, stock) {
  const stockInfo = stock
    ? `股票：${stock.name}（${stock.sid}）
昨收：${stock.close}，漲跌幅：${stock.change_pct}%
評分：${stock.score}／100，系統估算機率：${stock.prob}
MA20：${stock.ma20 ?? "無資料"}
三大法人合計：${stock.inst?.total_net ?? "無資料"} 張
波動度：${stock.risk?.volatility_level ?? "無資料"}
乖離率：${stock.risk?.bias20_pct ?? "無資料"}%
連續上漲天數：${stock.risk?.up_streak_days ?? "無資料"}`
    : `查無「${query}」的今日掃描資料，請根據你既有的知識，提醒使用者這支股票
不在今日掃描範圍內，並給出一般性的觀察重點。`;

  return `你是台股權證操作的分析助理。根據以下資料，評估這支股票明天「續航」（延續今天強勢）的機率，並給出 3-5 點具體理由。

${stockInfo}

請「只」回傳以下格式的 JSON，不要有任何其他文字、不要用 markdown code block：
{"probability":"約 XX%","summary":"一句話總結","reasons":["理由1","理由2","理由3"]}`;
}

async function callGemini(model, apiKey, prompt) {
  const url = `${GEMINI_API_BASE}/${model}:generateContent?key=${apiKey}`;
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      contents: [{ parts: [{ text: prompt }] }],
      generationConfig: { temperature: 0.4, maxOutputTokens: 600 },
    }),
  });

  if (!res.ok) {
    throw new Error(`HTTP ${res.status}`);
  }

  const data = await res.json();
  const rawText = data.candidates?.[0]?.content?.parts?.[0]?.text ?? "";
  if (!rawText) throw new Error("空回應");

  const cleaned = rawText.replace(/```json|```/g, "").trim();
  const parsed = JSON.parse(cleaned);
  if (!parsed.probability) throw new Error("回應格式不正確");
  return parsed;
}
