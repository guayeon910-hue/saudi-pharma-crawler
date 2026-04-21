/**
 * UPharma Export AI — 사우디아라비아 대시보드 스크립트
 * ═══════════════════════════════════════════════════════════════
 *
 * 기능 목록:
 *   §1  상수 & 전역 상태
 *   §2  탭 전환          goTab(id, el)
 *   §3  환율 로드        loadExchange()  → GET /api/exchange
 *   §4  To-Do 리스트     initTodo / toggleTodo / markTodoDone / addTodoItem
 *   §5  보고서 탭        renderReportTab / _addReportEntry
 *   §6  API 키 배지      loadKeyStatus() → GET /api/keys/status
 *   §7  진행 단계        setProgress / resetProgress
 *   §8  파이프라인       runPipeline / pollPipeline
 *   §9  신약 분석        runCustomPipeline / _pollCustomPipeline
 *   §10 결과 렌더링      renderResult
 *   §11 시장 뉴스
 *   §12 2공정 UI
 *   §13 초기화
 *
 * 수정 이력:
 *   B1  /api/sites 제거 → /api/datasource/status
 *   B2  크롤링 step → DB 조회 step (prog-db_load)
 *   B3  refreshOutlier → /api/analyze/result
 *   B4  논문 카드: refs 0건이면 숨김
 *   U1  API 키 상태 배지
 *   U2  진입 경로(entry_pathway) 표시
 *   U3  신뢰도(confidence_note) 표시
 *   U4  PDF 카드 3가지 상태
 *   U6  재분석 버튼
 *   N1  탭 전환
 *   N2  환율 카드 (SAR/KRW)
 *   N3  To-Do 리스트 (localStorage)
 *   N4  보고서 탭 자동 등록
 *   SA1 SG → SA 전환 (레지스트리 ID 대시형, SAR 환율, SFDA 규제)
 * ═══════════════════════════════════════════════════════════════
 */

'use strict';

/* ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   §1. 상수 & 전역 상태
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */

/**
 * product_id → INN 표시명
 * value = drug_registry.json 의 id 필드 (대시형 slug) 와 1:1 매칭
 */
const INN_MAP = {
  'hydrine':            'Hydroxyurea 500mg',
  'gadvoa-inj':         'Gadobutrol 604mg',
  'sereterol-activair': 'Fluticasone / Salmeterol',
  'omethyl-cutielet':   'Omega-3 EE 2g',
  'rosumeg-combigel':   'Rosuvastatin + Omega-3',
  'atmeg-combigel':     'Atorvastatin + Omega-3',
  'ciloduo':            'Cilostazol + Rosuvastatin',
  'gastiin-cr':         'Mosapride CR',
};

/**
 * B2: 서버 step 이름 → 프론트 progress 단계 ID 매핑
 * 서버 step: init → db_load → analyze → refs → report → done
 */
const STEP_ORDER = ['db_load', 'analyze', 'refs', 'report'];

const PIPELINE_POLL_MS = 2500;

let _pollTimer  = null;   // 파이프라인 폴링 예약(timeout) — setTimeout 체인만 사용
let _currentKey = null;   // 현재 선택된 product_key
/** 새 runPipeline 호출 시 증가. 오래된 폴링 콜백은 무시한다 */
let _pipelineGen = 0;
/** 폴링 완료 분기 1회 처리(레거시 보조) */
let _pipelineHandlingDone = false;

let _customPollTimer = null;
let _customPipelineGen = 0;
let _customPipelineHandlingDone = false;

function _clearPipelinePollTimer() {
  if (_pollTimer != null) {
    clearTimeout(_pollTimer);
    clearInterval(_pollTimer);
    _pollTimer = null;
  }
}

function _clearCustomPollTimer() {
  if (_customPollTimer != null) {
    clearTimeout(_customPollTimer);
    clearInterval(_customPollTimer);
    _customPollTimer = null;
  }
}

/* ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   §2. 탭 전환 (N1)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

 공정 접기/펼치기 — .process-section 헤더 클릭 시 호출
 process-body.hidden 토글, process-arrow.closed 토글 */
function toggleProcess(id) {
  const body  = document.getElementById('pb-' + id);
  const arrow = document.getElementById('pa-' + id);
  if (!body) return;
  const hidden = body.classList.toggle('hidden');
  if (arrow) arrow.classList.toggle('closed', hidden);
}

/* 사우디 거시지표 — 하드코딩 (API 연동 시 /api/macro 로 교체) */
function loadMacro() {
  const data = {
    gdp:        '$32,000',   gdp_src:    '2024 · IMF / GASTAT',
    pop:        '37.2M',     pop_src:    '2024 · GASTAT',
    pharma:     '$8.8B',     pharma_src: '2024 · IQVIA',
    growth:     '2.6%',      growth_src: '2024 · SAMA',
  };
  const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
  set('macro-gdp',        data.gdp);
  set('macro-gdp-src',    data.gdp_src);
  set('macro-pop',        data.pop);
  set('macro-pop-src',    data.pop_src);
  set('macro-pharma',     data.pharma);
  set('macro-pharma-src', data.pharma_src);
  set('macro-growth',     data.growth);
  set('macro-growth-src', data.growth_src);
}

/* P2 함수 호환 래퍼 — HTML onclick 참조와 app.js 구현을 분리 */
function runP2AiPipeline()   { return runP2PriceAnalysis(); }
function setP2AiSeg(seg, el) { return setP2Market(seg, el); }

/* ── 로딩 상태 헬퍼 (싱가포르 동일 패턴) ── */
function _showP1Loading()     { const el = document.getElementById('p1-loading-state');     if (el) el.style.display = 'flex'; }
function _hideP1Loading()     { const el = document.getElementById('p1-loading-state');     if (el) el.style.display = 'none'; }
function _showCustomLoading() { const el = document.getElementById('custom-loading-state'); if (el) el.style.display = 'flex'; }
function _hideCustomLoading() { const el = document.getElementById('custom-loading-state'); if (el) el.style.display = 'none'; }
function _showP2Loading()     { const el = document.getElementById('p2-loading-state');     if (el) el.style.display = 'flex'; }
function _hideP2Loading()     { const el = document.getElementById('p2-loading-state');     if (el) el.style.display = 'none'; }

/* 신약 직접 분석 폼 토글 */
function toggleCustomForm() {
  const wrap = document.getElementById('custom-form-wrap');
  const btn  = document.getElementById('btn-custom-toggle');
  if (!wrap) return;
  const open = wrap.style.display === 'none' || wrap.style.display === '';
  wrap.style.display = open ? 'block' : 'none';
  if (btn) btn.textContent = (open ? '▾' : '▸') + ' 신약 직접 분석';
}

/**
 * 사우디 전용: 경쟁사 맵 + White-Space 자동 실행
 * 1공정 완료(renderResult) 또는 바이어 발굴 완료(_pollP3) 시 호출.
 */
function _autoRunSaudiPanels(productKey) {
  const pk  = productKey || _currentKey || document.getElementById('product-select')?.value || null;
  if (!pk) return;

  // ① 경쟁사 유통 구도 — 자동 실행
  try {
    const inn = INN_MAP[pk] || null;
    loadCompetitorMap({ product_key: pk, target_inn: inn });
  } catch (_) { /* swallow */ }

  // ② White-Space 분석 — INN 자동 입력 후 실행
  try {
    const rawInn = INN_MAP[pk] || '';
    const innFirst = rawInn.split(/\s*[\/+]\s*/)[0].trim();
    if (!innFirst) return;
    const innEl = document.getElementById('p3-ws-inn');
    if (innEl) innEl.value = innFirst;
    const wsCard = document.getElementById('p3-whitespace-card');
    if (wsCard) wsCard.style.display = '';
    runP3WhiteSpace();
  } catch (_) { /* swallow */ }
}

/**
 * 탭 전환: 모든 .page / .tab 비활성 후 대상만 활성화.
 * @param {string} id  — 대상 페이지 element ID
 * @param {Element} el — 클릭된 탭 element
 */
function goTab(id, el) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('on'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('on'));
  const page = document.getElementById(id);
  if (page) page.classList.add('on');
  const tabEl = el || document.getElementById('tab-' + id);
  if (tabEl) tabEl.classList.add('on');
  if (id === 'main') populateP2ReportSelect();
}

/* ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   §3. 환율 로드 (N2) — GET /api/exchange
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */

async function loadExchange() {
  const btn = document.getElementById('btn-exchange-refresh');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ 조회 중…'; }

  try {
    const res  = await fetch('/api/exchange');
    const data = await res.json();

    // 메인 숫자 (KRW/SAR) — /api/exchange 의 sar_krw 필드
    const rateEl = document.getElementById('exchange-main-rate');
    if (rateEl) {
      const fmt = data.sar_krw.toLocaleString('ko-KR', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
      rateEl.innerHTML = `${fmt}<span style="font-size:14px;margin-left:4px;color:var(--muted);font-weight:700;">원</span>`;
    }

    // 서브 그리드 (USD/KRW, SAR/USD)
    const subEl = document.getElementById('exchange-sub');
    if (subEl) {
      const fmtUsd = data.usd_krw.toLocaleString('ko-KR', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
      subEl.innerHTML = `
        <div class="irow" style="margin:0">
          <div style="font-size:10.5px;color:var(--muted);margin-bottom:3px;">USD / KRW</div>
          <div style="font-size:15px;font-weight:900;color:var(--navy);">${fmtUsd}원</div>
        </div>
        <div class="irow" style="margin:0">
          <div style="font-size:10.5px;color:var(--muted);margin-bottom:3px;">SAR / USD</div>
          <div style="font-size:15px;font-weight:900;color:var(--navy);">${data.sar_usd.toFixed(4)}</div>
        </div>
      `;
    }

    // 출처 + 조회 시각 (source: yfinance | cache | cache_stale | fallback)
    const srcEl = document.getElementById('exchange-source');
    if (srcEl) {
      const now = new Date().toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit' });
      const srcLabel = ({
        http_ecb:     'ECB(경유) · SAR/USD 페그',
        yfinance:    'Yahoo Finance',
        cache:       '실시간 시세 · 캐시',
        cache_stale: '실시간 시세 · 이전 캐시',
        fallback:    '폴백값 (API 실패)',
      })[data.source] || (data.ok ? '실시간' : '폴백값');
      srcEl.textContent = `${srcLabel} · ${now}`;
    }
  } catch (e) {
    const srcEl = document.getElementById('exchange-source');
    if (srcEl) srcEl.textContent = '환율 조회 실패 — 잠시 후 다시 시도해 주세요';
    console.warn('환율 로드 실패:', e);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '↺ 환율 새로고침'; }
  }
}

/* ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   §4. To-Do 리스트 (N3)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */

const TODO_FIXED_IDS = ['p1', 'rep', 'p2', 'p3'];
const TODO_LS_KEY    = 'sa_upharma_todos_v1';
let _lastTodoAddAt   = 0;

/** localStorage에서 todo 상태 읽기 */
function _loadTodoState() {
  try   { return JSON.parse(localStorage.getItem(TODO_LS_KEY) || '{}'); }
  catch { return {}; }
}

/** localStorage에 todo 상태 쓰기 */
function _saveTodoState(state) {
  localStorage.setItem(TODO_LS_KEY, JSON.stringify(state));
}

/** 페이지 로드 시 localStorage 상태 복원 */
function initTodo() {
  const state = _loadTodoState();

  // 고정 항목 상태 복원
  for (const id of TODO_FIXED_IDS) {
    const item = document.getElementById('todo-' + id);
    if (!item) continue;
    item.classList.toggle('done', !!state['fixed_' + id]);
  }

  // 커스텀 항목 렌더
  _renderCustomTodos(state);
}

/**
 * 고정 항목 수동 토글 (클릭 시 호출).
 * @param {string} id  'p1' | 'rep' | 'p2' | 'p3'
 */
function toggleTodo(id) {
  const state       = _loadTodoState();
  const key         = 'fixed_' + id;
  state[key]        = !state[key];
  _saveTodoState(state);

  const item = document.getElementById('todo-' + id);
  if (item) item.classList.toggle('done', state[key]);
}

/**
 * 자동 체크: 파이프라인·보고서 완료 시 호출 (N3).
 * @param {'p1'|'rep'} id
 */
function markTodoDone(id) {
  const state       = _loadTodoState();
  state['fixed_' + id] = true;
  _saveTodoState(state);

  const item = document.getElementById('todo-' + id);
  if (item) item.classList.add('done');
}

/** 사용자가 직접 항목 추가 */
function addTodoItem(evt) {
  if (evt) {
    if (evt.isComposing || evt.repeat) return;
    evt.preventDefault();
  }

  const now = Date.now();
  if (now - _lastTodoAddAt < 250) return;
  _lastTodoAddAt = now;

  const input = document.getElementById('todo-input');
  const text  = input ? input.value.trim() : '';
  if (!text) return;

  const state   = _loadTodoState();
  const customs = state.customs || [];
  customs.push({ id: now + Math.floor(Math.random() * 1000), text, done: false });
  state.customs = customs;
  _saveTodoState(state);
  _renderCustomTodos(state);
  if (input) input.value = '';
}

/** 커스텀 항목 토글 */
function toggleCustomTodo(id) {
  const state   = _loadTodoState();
  const customs = state.customs || [];
  const item    = customs.find(c => c.id === id);
  if (item) item.done = !item.done;
  state.customs = customs;
  _saveTodoState(state);
  _renderCustomTodos(state);
}

/** 커스텀 항목 삭제 */
function deleteCustomTodo(id) {
  const state   = _loadTodoState();
  state.customs = (state.customs || []).filter(c => c.id !== id);
  _saveTodoState(state);
  _renderCustomTodos(state);
}

/** 커스텀 항목 목록 DOM 갱신 */
function _renderCustomTodos(state) {
  const container = document.getElementById('todo-custom-list');
  if (!container) return;
  container.classList.add('todo-list');

  const customs = state.customs || [];
  if (!customs.length) { container.innerHTML = ''; return; }

  container.innerHTML = customs.map(c => `
    <div class="todo-item${c.done ? ' done' : ''}" onclick="toggleCustomTodo(${c.id})">
      <div class="todo-check"></div>
      <span class="todo-label">${_escHtml(c.text)}</span>
      <button
        onclick="event.stopPropagation();deleteCustomTodo(${c.id})"
        style="background:none;color:var(--muted);font-size:16px;cursor:pointer;
               border:none;outline:none;padding:0 4px;line-height:1;flex-shrink:0;"
        title="삭제"
      >×</button>
    </div>
  `).join('');
}

/* ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   §5. 보고서 탭 관리 (N4)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */

const REPORTS_LS_KEY = 'sa_upharma_reports_v1';
const REPORTS_FULL_LS_KEY = 'sa_upharma_reports_full_v1';

/** _addReportEntry 직후 연속 동일 호출 방지(ms 이내 동일 품목·PDF) */
let _lastReportDedupe = { fp: '', t: 0 };

let _p2LastRequestState = null;
let _p2OverrideTimer = null;
let _p2Running = false;

function _loadReportFullStore() {
  try   { return JSON.parse(localStorage.getItem(REPORTS_FULL_LS_KEY) || '{}'); }
  catch { return {}; }
}

function _saveReportFull(id, result, keptIds) {
  if (!result) return;
  try {
    const store = _loadReportFullStore();
    store[String(id)] = {
      trade_name: result.trade_name || null,
      inn: result.inn || null,
      product_id: result.product_id || null,
      dosage_form: result.dosage_form || null,
      strength: result.strength || null,
      price_sar: result.price_sar ?? null,
      estimated_avg_sar: (result.price_comparison?.estimated?.avg_sar ?? null),
      price_comparison: result.price_comparison || null,
      verdict: result.verdict || null,
      hs_code: result.hs_code || null,
    };

    const keep = new Set((keptIds || []).map(v => String(v)));
    Object.keys(store).forEach(key => {
      if (!keep.has(key)) delete store[key];
    });
    localStorage.setItem(REPORTS_FULL_LS_KEY, JSON.stringify(store));
  } catch (e) {
    console.warn('full report save failed:', e);
  }
}

function _loadReportFull(id) {
  const store = _loadReportFullStore();
  return store[String(id)] || null;
}

function _loadReports() {
  try   { return JSON.parse(localStorage.getItem(REPORTS_LS_KEY) || '[]'); }
  catch { return []; }
}

function _loadReportsFull() {
  try   { return JSON.parse(localStorage.getItem(REPORTS_FULL_LS_KEY) || '{}'); }
  catch { return {}; }
}

function _saveReportsFull(map) {
  try { localStorage.setItem(REPORTS_FULL_LS_KEY, JSON.stringify(map || {})); }
  catch (e) { console.warn('보고서 full blob 저장 실패:', e); }
}

/**
 * 2공정 파이프라인이 실제로 사용할 최소 full blob.
 * result 전체를 그대로 저장하면 용량이 크므로 필요한 필드만 뽑는다.
 */
function _buildReportFullBlob(result) {
  if (!result || typeof result !== 'object') return null;
  const pc   = result.price_comparison || {};
  const same = Array.isArray(pc.same_ingredient) ? pc.same_ingredient : [];
  const comp = Array.isArray(pc.competitors)     ? pc.competitors     : [];
  const keep = rows => rows.slice(0, 30).map(r => ({
    trade_name: r.trade_name || '',
    strength:   r.strength   || '',
    price:      (typeof r.price === 'number') ? r.price : null,
    currency:   r.currency || 'SAR',
    source:     r.source   || '',
    type:       r.type     || '',
  }));
  return {
    trade_name:  result.trade_name  || result.product_id || '',
    ingredient:  result.inn         || result.ingredient || '',
    strength:    result.strength    || '',
    dosage_form: result.dosage_form || '',
    hs_code:     result.hs_code     || null,
    price_sar:   (result.price_comparison?.summary?.avg ?? null),
    price_comparison: {
      same_ingredient: keep(same),
      competitors:     keep(comp),
    },
  };
}

/**
 * 1공정 완료 후 renderResult()가 호출 → 보고서 탭에 항목 추가.
 * @param {object|null} result  분석 결과
 * @param {string|null} pdfName PDF 파일명
 */
function _addReportEntry(result, pdfName, reportType) {
  const prodKey = result ? String(result.product_id || result.trade_name || '') : '';
  const pdfPart = pdfName ? String(pdfName).replace(/^.*[/\\]/, '') : '';
  const fp = `${prodKey}|${pdfPart}`;
  const now = Date.now();
  if (_lastReportDedupe.fp === fp && (now - _lastReportDedupe.t) < 5000) {
    populateP2ReportSelect();
    _syncP3ReportOptions();
    return;
  }
  _lastReportDedupe = { fp, t: now };

  const reports = _loadReports();
  const entryId = Date.now();
  const entry   = {
    id:          entryId,
    report_type: reportType || 'p1',
    product:     result ? (result.trade_name || result.product_id || '알 수 없음') : '알 수 없음',
    inn:         result ? (INN_MAP[result.product_id] || result.inn || '') : '',
    verdict:     result ? (result.verdict || '—') : '—',
    timestamp:   new Date().toLocaleString('ko-KR', {
      month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit',
    }),
    hasPdf: !!pdfName,
  };

  reports.unshift(entry);
  const trimmed = reports.slice(0, 30);
  localStorage.setItem(REPORTS_LS_KEY, JSON.stringify(trimmed));
  _saveReportFull(entryId, result, trimmed.map(r => r.id));
  renderReportTab();
  _syncP3ReportOptions();
}

/** 보고서 탭 DOM 갱신 */
function renderReportTab() {
  const container = document.getElementById('report-tab-list');
  if (!container) return;

  const reports = _loadReports();
  if (!reports.length) {
    container.innerHTML = `
      <div class="rep-empty">
        아직 생성된 보고서가 없습니다.<br>
        1공정 분석을 실행하면 여기에 자동으로 등록됩니다.
      </div>`;
    return;
  }

  container.innerHTML = reports.map(r => {
    const vc = r.verdict === '적합'   ? 'green'
             : r.verdict === '부적합' ? 'red'
             : r.verdict !== '—'      ? 'orange'
             :                          'gray';
    const innSpan = r.inn
      ? ` <span style="font-weight:400;color:var(--muted);font-size:12px;">· ${_escHtml(r.inn)}</span>`
      : '';
    const dlBtn = r.hasPdf
      ? `<a class="btn-download"
            href="/api/report/download"
            target="_blank"
            style="padding:7px 14px;font-size:12px;flex-shrink:0;">📄 PDF</a>`
      : '';

    return `
      <div class="rep-item">
        <div class="rep-item-info">
          <div class="rep-item-product">${_escHtml(r.product)}${innSpan}</div>
          <div class="rep-item-meta">${_escHtml(r.timestamp)}</div>
        </div>
        <div class="rep-item-verdict">
          <span class="bdg ${vc}">${_escHtml(r.verdict)}</span>
        </div>
        ${dlBtn}
      </div>`;
  }).join('');
}

/* ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   §6. API 키 상태 (U1) — GET /api/keys/status
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */

async function loadKeyStatus() {
  try {
    const res  = await fetch('/api/keys/status');
    const data = await res.json();
    _applyKeyBadge('key-claude',     data.claude,     'Claude',     'API 키 설정됨',  'API 키 미설정 — 분석 불가');
    _applyKeyBadge('key-perplexity', data.perplexity, 'Perplexity', 'API 키 설정됨',  '미설정 — 논문 검색 생략');
  } catch (_) { /* 조용히 실패 */ }
}

function _applyKeyBadge(id, active, label, okTitle, ngTitle) {
  const el = document.getElementById(id);
  if (!el) return;
  el.className = 'key-badge ' + (active ? 'active' : 'inactive');
  el.title     = active ? `${label} ${okTitle}` : `${label} ${ngTitle}`;
  const dot    = el.querySelector('.key-badge-dot');
  if (dot) dot.style.background = active ? 'var(--green)' : 'var(--muted)';
}

/* ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   §7. 진행 단계 표시 (B2)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */

/**
 * @param {string} currentStep  STEP_ORDER 내 현재 단계
 * @param {'running'|'done'|'error'} status
 */
function setProgress(currentStep, status) {
  const row = document.getElementById('progress-row');
  if (row) row.classList.add('visible');
  const idx = STEP_ORDER.indexOf(currentStep);

  for (let i = 0; i < STEP_ORDER.length; i++) {
    const el  = document.getElementById('prog-' + STEP_ORDER[i]);
    if (!el) continue;
    const dot = el.querySelector('.prog-dot');

    if (status === 'error' && i === idx) {
      el.className    = 'prog-step error';
      dot.textContent = '✕';
    } else if (i < idx || (i === idx && status === 'done')) {
      el.className    = 'prog-step done';
      dot.textContent = '✓';
    } else if (i === idx) {
      el.className    = 'prog-step active';
      dot.textContent = i + 1;
    } else {
      el.className    = 'prog-step';
      dot.textContent = i + 1;
    }
  }
}

function resetProgress() {
  const row = document.getElementById('progress-row');
  if (row) row.classList.remove('visible');
  for (let i = 0; i < STEP_ORDER.length; i++) {
    const el = document.getElementById('prog-' + STEP_ORDER[i]);
    if (!el) continue;
    el.className = 'prog-step';
    el.querySelector('.prog-dot').textContent = i + 1;
  }
}

/** 서버 FastAPI 오류 본문(detail)을 한 줄 문자열로 */
function _formatApiDetail(payload) {
  if (!payload || payload.detail === undefined || payload.detail === null) return '';
  const det = payload.detail;
  if (typeof det === 'string') return det;
  if (Array.isArray(det)) {
    return det.map((e) => (e && typeof e === 'object' && 'msg' in e ? e.msg : JSON.stringify(e))).join(' ');
  }
  return String(det);
}

function _isFileOrigin() {
  return window.location.protocol === 'file:';
}

function _showP1PipelineError(message) {
  const el = document.getElementById('p1-result-note');
  if (!el) return;
  el.style.display = 'block';
  el.className = 'p1-result-note err';
  el.textContent = message;
}

function _hideP1PipelineNote() {
  const el = document.getElementById('p1-result-note');
  if (!el) return;
  el.style.display = 'none';
  el.textContent = '';
  el.className = 'p1-result-note';
}

/** 파이프라인 오류 시 어느 progress 칸에 ✕를 넣을지 (구버전 서버 step=crawl 호환) */
function _pipelineErrorProgressStep(step, stepLabel) {
  if (step === 'db_load' || step === 'crawl') return 'db_load';
  if (STEP_ORDER.includes(step)) return step;
  if (step === 'error') {
    const lab = String(stepLabel || '');
    if (/크롤|DB|적재|Supabase|조회/i.test(lab)) return 'db_load';
    if (/Claude|분석/i.test(lab)) return 'analyze';
    if (/참고|논문|Perplexity|refs/i.test(lab)) return 'refs';
    if (/보고서|PDF|report/i.test(lab)) return 'report';
  }
  return 'analyze';
}

/* ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   §8. 파이프라인 실행 & 폴링
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */

/**
 * 선택 품목 파이프라인 실행.
 * U6: 재분석 버튼도 이 함수를 호출.
 */
async function runPipeline() {
  const productKey = document.getElementById('product-select').value;
  _currentKey      = productKey;

  _clearPipelinePollTimer();
  const gen = ++_pipelineGen;

  // UI 초기화
  resetProgress();
  document.getElementById('result-card').classList.remove('visible');
  document.getElementById('papers-card').classList.remove('visible');
  document.getElementById('report-card').classList.remove('visible');
  document.getElementById('btn-analyze').disabled = true;
  document.getElementById('btn-icon').textContent  = '⏳';
  _showP1Loading();

  const reBtn = document.getElementById('btn-reanalyze');
  if (reBtn) reBtn.style.display = 'none';

  _hideP1PipelineNote();

  _pipelineHandlingDone = false;

  // B2: db_load 단계 먼저 활성화
  setProgress('db_load', 'running');

  try {
    const res = await fetch(`/api/pipeline/${encodeURIComponent(productKey)}`, { method: 'POST' });
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      const detail = _formatApiDetail(d) || `HTTP ${res.status}`;
      console.error('파이프라인 오류:', detail);
      let msg = `파이프라인을 시작할 수 없습니다: ${detail}`;
      if (_isFileOrigin()) {
        msg = '페이지가 file:// 로 열려 API를 호출할 수 없습니다. 터미널에서 uvicorn을 실행한 뒤 주소창에 http://127.0.0.1:8000 을 입력하세요.';
      }
      _showP1PipelineError(msg);
      setProgress('db_load', 'error');
      _resetBtn();
      return;
    }
    _pollTimer = setTimeout(() => pollPipeline(productKey, gen), PIPELINE_POLL_MS);
  } catch (e) {
    console.error('요청 실패:', e);
    let msg = `서버에 연결하지 못했습니다 (${e && e.message ? e.message : '네트워크 오류'}). uvicorn이 http://127.0.0.1:8000 에서 실행 중인지 확인하세요.`;
    if (_isFileOrigin()) {
      msg = 'HTML 파일을 직접 연 경우(/로 시작하는 주소가 아닌 경우) 분석 API가 동작하지 않습니다. python -m uvicorn frontend.server:app --host 0.0.0.0 --port 8000 실행 후 브라우저에서 같은 포트로 접속하세요.';
    }
    _showP1PipelineError(msg);
    setProgress('db_load', 'error');
    _resetBtn();
  }
}

function _resetBtn() {
  document.getElementById('btn-analyze').disabled = false;
  document.getElementById('btn-icon').textContent  = '▶';
  _hideP1Loading();
}

/**
 * GET /api/pipeline/{product_key}/status 를 주기적으로 폴링.
 * 서버 step: init → db_load → analyze → refs → report → done
 */
async function pollPipeline(productKey, gen) {
  if (gen !== _pipelineGen) return;
  try {
    const res = await fetch(`/api/pipeline/${encodeURIComponent(productKey)}/status`);
    if (gen !== _pipelineGen) return;
    if (!res.ok) {
      _pipelineHandlingDone = false;
      _clearPipelinePollTimer();
      const raw = await res.json().catch(() => ({}));
      const detail = _formatApiDetail(raw) || `HTTP ${res.status}`;
      _showP1PipelineError(`상태 조회 실패: ${detail}. 서버를 재시작했거나 세션이 끊겼을 수 있습니다.`);
      setProgress('db_load', 'error');
      _resetBtn();
      return;
    }
    const d = await res.json();
    if (gen !== _pipelineGen) return;

    if (d.status === 'idle') {
      if (gen !== _pipelineGen) return;
      _pollTimer = setTimeout(() => pollPipeline(productKey, gen), PIPELINE_POLL_MS);
      return;
    }

    // B2: 서버 step → 프론트 STEP_ORDER 매핑 (구버전 step 이름 crawl 호환)
    if      (d.step === 'db_load' || d.step === 'crawl') {
      setProgress('db_load', 'running');
    }
    else if (d.step === 'analyze')  { setProgress('db_load',  'done'); setProgress('analyze', 'running'); }
    else if (d.step === 'refs')     { setProgress('analyze',  'done'); setProgress('refs',    'running'); }
    else if (d.step === 'report')   {
      setProgress('refs', 'done'); setProgress('report', 'running');
      _showReportLoading();
    }

    if (d.status === 'done') {
      if (gen !== _pipelineGen) return;
      if (_pipelineHandlingDone) return;
      _pipelineHandlingDone = true;
      _clearPipelinePollTimer();
      _hideP1PipelineNote();
      for (const s of STEP_ORDER) setProgress(s, 'done');
      const r2   = await fetch(`/api/pipeline/${encodeURIComponent(productKey)}/result`);
      if (gen !== _pipelineGen) return;
      const data = await r2.json();
      renderResult(data.result, data.refs, data.pdf);
      _resetBtn();
      return;
    }

    if (d.status === 'error') {
      _pipelineHandlingDone = false;
      _clearPipelinePollTimer();
      const errStep = _pipelineErrorProgressStep(d.step, d.step_label);
      setProgress(errStep, 'error');
      const hint = d.step_label ? String(d.step_label) : '알 수 없는 오류';
      _showP1PipelineError(`파이프라인 오류: ${hint}`);
      _resetBtn();
      return;
    }

    if (gen !== _pipelineGen) return;
    _pollTimer = setTimeout(() => pollPipeline(productKey, gen), PIPELINE_POLL_MS);
  } catch (_) {
    if (gen !== _pipelineGen) return;
    _pollTimer = setTimeout(() => pollPipeline(productKey, gen), PIPELINE_POLL_MS);
  }
}

/* ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   §9. 신약 분석 파이프라인
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */

const CUSTOM_STEP_ORDER = ['analyze', 'refs', 'report'];

function _setCustomProgress(step, status) {
  const row = document.getElementById('custom-progress-row');
  if (row) row.classList.add('visible');
  const idMap = { analyze: 'cprog-analyze', refs: 'cprog-refs', report: 'cprog-report' };
  const idx   = CUSTOM_STEP_ORDER.indexOf(step);

  CUSTOM_STEP_ORDER.forEach((s, i) => {
    const el  = document.getElementById(idMap[s]);
    if (!el) return;
    const dot = el.querySelector('.prog-dot');
    if (status === 'error' && i === idx) {
      el.className = 'prog-step error'; dot.textContent = '✕';
    } else if (i < idx || (i === idx && status === 'done')) {
      el.className = 'prog-step done';  dot.textContent = '✓';
    } else if (i === idx) {
      el.className = 'prog-step active'; dot.textContent = i + 1;
    } else {
      el.className = 'prog-step'; dot.textContent = i + 1;
    }
  });
}

function _resetCustomProgress() {
  const row = document.getElementById('custom-progress-row');
  if (row) row.classList.remove('visible');
  CUSTOM_STEP_ORDER.forEach((s, i) => {
    const el = document.getElementById('cprog-' + s);
    if (!el) return;
    el.className = 'prog-step';
    el.querySelector('.prog-dot').textContent = i + 1;
  });
}

function _resetCustomBtn() {
  document.getElementById('btn-custom').disabled = false;
  document.getElementById('custom-icon').textContent = '▶';
  _hideCustomLoading();
}

async function runCustomPipeline() {
  const tradeName = document.getElementById('custom-trade-name').value.trim();
  const inn       = document.getElementById('custom-inn').value.trim();
  const dosage    = document.getElementById('custom-dosage').value.trim();
  if (!tradeName || !inn) { alert('약품명과 성분명을 입력하세요.'); return; }

  _clearCustomPollTimer();
  const cgen = ++_customPipelineGen;

  _resetCustomProgress();
  document.getElementById('result-card').classList.remove('visible');
  document.getElementById('papers-card').classList.remove('visible');
  document.getElementById('report-card').classList.remove('visible');
  document.getElementById('btn-custom').disabled = true;
  document.getElementById('custom-icon').textContent = '⏳';
  _showCustomLoading();

  _customPipelineHandlingDone = false;
  _setCustomProgress('analyze', 'running');

  try {
    const res = await fetch('/api/pipeline/custom', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ trade_name: tradeName, inn, dosage_form: dosage }),
    });
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      console.error('신약 분석 오류:', d.detail || res.status);
      _setCustomProgress('analyze', 'error');
      _resetCustomBtn();
      return;
    }
    _customPollTimer = setTimeout(() => _pollCustomPipeline(cgen), PIPELINE_POLL_MS);
  } catch (e) {
    console.error('요청 실패:', e);
    _setCustomProgress('analyze', 'error');
    _resetCustomBtn();
  }
}

async function _pollCustomPipeline(gen) {
  if (gen !== _customPipelineGen) return;
  try {
    const res = await fetch('/api/pipeline/custom/status');
    if (gen !== _customPipelineGen) return;
    const d   = await res.json();
    if (gen !== _customPipelineGen) return;
    if (d.status === 'idle') {
      if (gen !== _customPipelineGen) return;
      _customPollTimer = setTimeout(() => _pollCustomPipeline(gen), PIPELINE_POLL_MS);
      return;
    }

    if      (d.step === 'analyze') { _setCustomProgress('analyze', 'running'); }
    else if (d.step === 'refs')    { _setCustomProgress('analyze', 'done'); _setCustomProgress('refs', 'running'); }
    else if (d.step === 'report')  { _setCustomProgress('refs', 'done'); _setCustomProgress('report', 'running'); _showReportLoading(); }

    if (d.status === 'done') {
      if (gen !== _customPipelineGen) return;
      if (_customPipelineHandlingDone) return;
      _customPipelineHandlingDone = true;
      _clearCustomPollTimer();
      for (const s of CUSTOM_STEP_ORDER) _setCustomProgress(s, 'done');
      const r2   = await fetch('/api/pipeline/custom/result');
      if (gen !== _customPipelineGen) return;
      const data = await r2.json();
      renderResult(data.result, data.refs, data.pdf);
      _resetCustomBtn();
      return;
    }
    if (d.status === 'error') {
      _customPipelineHandlingDone = false;
      _clearCustomPollTimer();
      _setCustomProgress(d.step || 'analyze', 'error');
      _resetCustomBtn();
      return;
    }

    if (gen !== _customPipelineGen) return;
    _customPollTimer = setTimeout(() => _pollCustomPipeline(gen), PIPELINE_POLL_MS);
  } catch (_) {
    if (gen !== _customPipelineGen) return;
    _customPollTimer = setTimeout(() => _pollCustomPipeline(gen), PIPELINE_POLL_MS);
  }
}

/* ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   §10. 결과 렌더링 (U2·U3·U4·U6·B4·N3·N4)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */

/**
 * 분석 완료 후 결과·논문·PDF 카드를 화면에 렌더링.
 * @param {object|null} result  분석 결과
 * @param {Array}       refs    Perplexity 논문 목록
 * @param {string|null} pdfName PDF 파일명
 */
function renderResult(result, refs, pdfName) {

  /* ─ 분석 결과 카드 ─ */
  if (result) {
    const verdict = result.verdict;
    const vc      = verdict === '적합'   ? 'v-ok'
                  : verdict === '부적합' ? 'v-err'
                  : verdict             ? 'v-warn'
                  :                       'v-none';
    const err    = result.analysis_error;
    const vLabel = verdict
      || (err === 'no_api_key'    ? 'API 키 미설정'
        : err === 'claude_failed' ? 'Claude 분석 실패'
        :                           '미분석');

    document.getElementById('verdict-badge').className   = `verdict-badge ${vc}`;
    document.getElementById('verdict-badge').textContent = vLabel;
    document.getElementById('verdict-name').textContent  = result.trade_name || result.product_id || '';
    document.getElementById('verdict-inn').textContent   = INN_MAP[result.product_id] || result.inn || '';

    // S2: 신호등
    ['tl-red', 'tl-yellow', 'tl-green'].forEach(id => {
      document.getElementById(id).classList.remove('on');
    });
    if (verdict === '적합')        document.getElementById('tl-green').classList.add('on');
    else if (verdict === '부적합') document.getElementById('tl-red').classList.add('on');
    else if (verdict)              document.getElementById('tl-yellow').classList.add('on');

    // S3: 판정 근거
    const basisFallback = _deriveBasisFromRationale(result.rationale);
    _setText('basis-market-medical', _formatDetailed(result.basis_market_medical || basisFallback.marketMedical));
    _setText('basis-regulatory',     _formatDetailed(result.basis_regulatory     || basisFallback.regulatory));
    _setText('basis-trade',          _formatDetailed(result.basis_trade          || basisFallback.trade));
    _setText('basis-pbs-line',       _pbsLineFromApi(result));

    // S4: 진입 채널
    const pathEl = document.getElementById('entry-pathway');
    if (pathEl) {
      if (result.entry_pathway) {
        pathEl.textContent   = result.entry_pathway;
        pathEl.style.display = 'inline-block';
      } else {
        pathEl.style.display = 'none';
      }
    }

    const pbsPos = String(result.price_positioning_pbs || '').trim();
    _setText('price-positioning-pbs', _formatDetailed(pbsPos || _pbsLineFromApi(result)));

    const riskText = String(result.risks_conditions || '').trim()
      || (Array.isArray(result.key_factors) ? result.key_factors.join(' / ') : '');
    _setText('risks-conditions', _formatDetailed(riskText));

    // U6: 재분석 버튼 표시
    const reBtn = document.getElementById('btn-reanalyze');
    if (reBtn) reBtn.style.display = 'inline-flex';

    document.getElementById('result-card').classList.add('visible');

    const p1Note = document.getElementById('p1-result-note');
    if (p1Note) {
      const displayName = result.trade_name || result.product_id || '분석';
      const verdictLabel = result.verdict || '—';
      p1Note.style.display = 'block';
      p1Note.className = 'p1-result-note';
      p1Note.textContent =
        `✅ ${displayName} 분석 완료 — 판정: ${verdictLabel}. 상세 결과는 보고서 탭·아래 PDF 카드에서 확인할 수 있습니다.`;
    }

    // N3: 1공정 완료 → Todo 자동 체크
    markTodoDone('p1');

    // 사우디 전용: 경쟁사 맵 + White-Space 자동 실행
    try { _autoRunSaudiPanels(result.product_id || result.product_key || null); } catch (_) {}
  }

  /* ─ B4: 논문 카드 ─ */
  const papersCard = document.getElementById('papers-card');
  const papersList = document.getElementById('papers-list');
  papersList.innerHTML = '';

  if (refs && refs.length > 0) {
    for (const ref of refs) {
      const item     = document.createElement('div');
      item.className = 'paper-item';
      const safeUrl  = /^https?:\/\//.test(ref.url || '') ? ref.url : '#';
      item.innerHTML = `
        <span class="paper-arrow">▸</span>
        <div>
          <div>
            <a class="paper-link" href="${safeUrl}" target="_blank" rel="noopener noreferrer"></a>
            <span class="paper-src"></span>
          </div>
          <div class="paper-reason"></div>
        </div>`;
      item.querySelector('.paper-link').textContent   = ref.title || ref.url || '';
      item.querySelector('.paper-src').textContent    = ref.source ? `[${ref.source}]` : '';
      item.querySelector('.paper-reason').textContent = ref.reason || '';
      papersList.appendChild(item);
    }
    papersCard.classList.add('visible');
  } else {
    papersCard.classList.remove('visible');
  }

  /* ─ U4: PDF 보고서 카드 ─ */
  if (pdfName) {
    _showReportOk();
    // N3: 보고서 완료 → Todo 자동 체크
    markTodoDone('rep');
    // N4: 보고서 탭에 자동 등록
    _addReportEntry(result, pdfName, 'p1');
    populateP2ReportSelect();
  } else {
    _showReportError();
  }
}

/** U4: PDF 생성 중 */
function _showReportLoading() {
  document.getElementById('report-state-loading').style.display = 'flex';
  document.getElementById('report-state-ok').style.display      = 'none';
  document.getElementById('report-state-error').style.display   = 'none';
  document.getElementById('report-card').classList.add('visible');
}

/** U4: PDF 생성 완료 */
function _showReportOk() {
  document.getElementById('report-state-loading').style.display = 'none';
  document.getElementById('report-state-ok').style.display      = 'block';
  document.getElementById('report-state-error').style.display   = 'none';
  document.getElementById('report-card').classList.add('visible');
}

/** U4: PDF 생성 실패 */
function _showReportError() {
  document.getElementById('report-state-loading').style.display = 'none';
  document.getElementById('report-state-ok').style.display      = 'none';
  document.getElementById('report-state-error').style.display   = 'block';
  document.getElementById('report-card').classList.add('visible');
}

/* ─ 유틸 함수 ─ */

function _setText(id, value, fallback = '—') {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = String(value || '').trim() || fallback;
}

function _deriveBasisFromRationale(rationale) {
  const text  = String(rationale || '');
  const lines = text.split('\n').map(x => x.trim()).filter(Boolean);
  const out   = { marketMedical: '', regulatory: '', trade: '' };
  for (const line of lines) {
    const low = line.toLowerCase();
    if (!out.marketMedical && (low.includes('시장') || low.includes('의료'))) {
      out.marketMedical = line.replace(/^[\-\d\.\)\s]+/, ''); continue;
    }
    if (!out.regulatory && low.includes('규제')) {
      out.regulatory = line.replace(/^[\-\d\.\)\s]+/, ''); continue;
    }
    if (!out.trade && low.includes('무역')) {
      out.trade = line.replace(/^[\-\d\.\)\s]+/, ''); continue;
    }
  }
  if (!out.marketMedical && lines.length > 0) out.marketMedical = lines[0];
  if (!out.regulatory    && lines.length > 1) out.regulatory    = lines[1];
  if (!out.trade         && lines.length > 2) out.trade         = lines[2];
  return out;
}

function _formatDetailed(text) {
  const src = String(text || '').trim();
  if (!src) return '';
  const lines   = src.split('\n').map(x => x.trim()).filter(Boolean);
  const cleaned = lines.map(l =>
    l.replace(/^[\-\•\*\·]\s+/, '').replace(/^\d+[\.\)]\s+/, '')
  );
  let joined = '';
  for (const part of cleaned) {
    if (!joined) { joined = part; continue; }
    const prev = joined.trimEnd();
    const ends = prev.endsWith('.') || prev.endsWith('!') || prev.endsWith('?')
              || prev.endsWith('다') || prev.endsWith('음') || prev.endsWith('임');
    joined += ends ? ' ' + part : ', ' + part;
  }
  return joined;
}

/**
 * 참고 가격 포맷 (SAR 중심, 레거시 AUD/PBS 필드도 호환)
 *   - result.price_sar        : 신규 사우디 가격 (1순위)
 *   - result.pbs_dpmq_aud     : 레거시 AU PBS (2순위, 변환 참고용)
 *   - result.pbs_haiku_estimate : Claude 추정 텍스트 (3순위)
 */
function _pbsLineFromApi(result) {
  // 1순위: 사우디 SAR 가격
  const sar    = result.price_sar;
  const sarNum = sar != null && sar !== '' ? Number(sar) : NaN;
  if (!Number.isNaN(sarNum)) {
    return `참고 SAR ${sarNum.toFixed(2)}`;
  }
  // 2순위: 레거시 AUD PBS (있으면 표기만)
  const aud    = result.pbs_dpmq_aud;
  const audNum = aud != null && aud !== '' ? Number(aud) : NaN;
  if (!Number.isNaN(audNum)) {
    return `참고 AUD ${audNum.toFixed(2)} (호주 PBS 레거시)`;
  }
  // 3순위: Claude 추정 텍스트
  const haiku = String(result.pbs_haiku_estimate || '').trim();
  if (haiku) return haiku;
  return '참고 가격 정보 없음';
}

/** XSS 방지 HTML 이스케이프 */
function _escHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/* ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   §11. 시장 신호 · 뉴스
   데이터 소스: Supabase `ai_discovered_sources` 테이블 (country=SA)
   폴백: SFDA / Vision 2030 정적 링크 (server.py)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */

async function loadNews() {
  const listEl = document.getElementById('news-list');
  const btn    = document.getElementById('btn-news-refresh');
  if (!listEl) return;

  if (btn) btn.disabled = true;
  listEl.innerHTML = '<div class="irow" style="color:var(--muted);font-size:12px;text-align:center;padding:20px 0;">뉴스 로드 중…</div>';

  try {
    const res  = await fetch('/api/news');
    const data = await res.json();

    if (!data.ok || !data.items?.length) {
      listEl.innerHTML = `<div class="irow" style="color:var(--muted);font-size:12px;text-align:center;padding:16px 0;">${data.error || '뉴스를 불러올 수 없습니다.'}</div>`;
      return;
    }

    listEl.innerHTML = data.items.map(item => {
      const href   = item.link ? `href="${_escHtml(item.link)}" target="_blank" rel="noopener"` : '';
      const tag    = item.link ? 'a' : 'div';
      const source = [item.source, item.date].filter(Boolean).join(' · ');
      return `
        <${tag} class="irow news-item" ${href} style="${item.link ? 'text-decoration:none;display:block;' : ''}">
          <div class="tit">${_escHtml(item.title)}</div>
          ${source ? `<div class="sub">${_escHtml(source)}</div>` : ''}
        </${tag}>`;
    }).join('');
  } catch (e) {
    listEl.innerHTML = '<div class="irow" style="color:var(--muted);font-size:12px;text-align:center;padding:16px 0;">뉴스 조회 실패 — 잠시 후 다시 시도해 주세요</div>';
    console.warn('뉴스 로드 실패:', e);
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   §12. 2공정 · 수출전략 (가격 분석 UI)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */

let _p2InputMode = 'ai';
let _p2Market    = 'public';

function _p2IsPrivateManualBlocked() {
  return _p2Market === 'private' && _p2InputMode === 'manual';
}

function _p2ResetResultView(clear = false) {
  const sec = document.getElementById('p2-ai-result-section');
  if (sec) sec.style.display = 'none';

  if (clear) {
    const list = document.getElementById('p2-product-list');
    if (list) list.innerHTML = '';
    const dl = document.getElementById('p2-report-dl-state');
    if (dl) dl.innerHTML = '';
    _p2LastRequestState = null;
    if (_p2OverrideTimer) clearTimeout(_p2OverrideTimer);
  }
}

function _p2UpdateManualNote() {
  const note = document.getElementById('p2-manual-note');
  if (!note) return;
  if (_p2InputMode !== 'manual') {
    note.style.display = 'none';
    return;
  }
  note.style.display = 'block';
  note.textContent = _p2Market === 'private'
    ? '민간 시장은 v1에서 직접 입력을 지원하지 않습니다. 저장된 1공정 보고서 또는 PDF를 사용하세요.'
    : '직접 입력은 공공 시장에서만 참고용으로 지원됩니다. (가격 표본이 없으면 분석이 거절될 수 있습니다.)';
}

function setP2InputMode(mode, el) {
  _p2InputMode = mode === 'manual' ? 'manual' : 'ai';
  const aiBtn = document.getElementById('p2-mode-ai');
  const mBtn  = document.getElementById('p2-mode-manual');
  if (aiBtn) aiBtn.classList.toggle('on', _p2InputMode === 'ai');
  if (mBtn)  mBtn.classList.toggle('on',  _p2InputMode === 'manual');

  const hAi = document.getElementById('p2-mode-hint-ai');
  const hM  = document.getElementById('p2-mode-hint-manual');
  if (hAi) hAi.style.display = _p2InputMode === 'ai' ? 'block' : 'none';
  if (hM)  hM.style.display  = _p2InputMode === 'manual' ? 'block' : 'none';

  const stepAi = document.getElementById('p2-step1-ai');
  const stepM  = document.getElementById('p2-step1-manual');
  if (stepAi) stepAi.style.display = _p2InputMode === 'ai' ? 'block' : 'none';
  if (stepM)  stepM.style.display  = _p2InputMode === 'manual' ? 'block' : 'none';

  _p2HideError();
  _p2ResetResultView(false);
  _p2UpdateManualNote();
  _p2UpdateRunEnabled();
}

function setP2Market(market, el) {
  _p2Market = market === 'private' ? 'private' : 'public';
  const pub = document.getElementById('p2-ai-seg-public');
  const prv = document.getElementById('p2-ai-seg-private');
  if (pub) pub.classList.toggle('on', _p2Market === 'public');
  if (prv) prv.classList.toggle('on', _p2Market === 'private');

  const segDesc = document.getElementById('p2-ai-seg-desc');
  if (segDesc) {
    segDesc.textContent = _p2Market === 'public'
      ? '공공 시장: NUPCO/SFDA 참고 가격 분포 기준 FOB 벤치마크 역산.'
      : '민간 시장: 병원·도매·소매 유통 기준 FOB 역산.';
  }

  _p2HideError();
  _p2ResetResultView(false);
  _p2UpdateManualNote();
  _p2UpdateRunEnabled();
}

function populateP2ReportSelect() {
  const sel = document.getElementById('p2-ai-report-select');
  if (!sel) return;
  const prev  = sel.value;
  const reports = _loadReports();
  const parts   = ['<option value="">저장된 1공정 보고서를 선택하세요</option>'];
  for (const r of reports) {
    const label = `시장조사 보고서 - ${_escHtml(r.product)}`;
    parts.push(`<option value="${String(r.id)}">${label}</option>`);
  }
  sel.innerHTML = parts.join('');
  if (prev && [...sel.options].some(o => o.value === prev)) sel.value = prev;
  _p2UpdateRunEnabled();
}

function _p2UpdatePdfLabel() {
  const inp = document.getElementById('p2-pdf-input');
  const lab = document.getElementById('p2-pdf-label');
  if (!inp || !lab) return;
  const f = inp.files && inp.files[0];
  lab.textContent = f ? f.name : '';
}

function _p2HideError() {
  const el = document.getElementById('p2-ai-error-msg');
  if (el) { el.style.display = 'none'; el.textContent = ''; }
}

function _p2ShowError(msg) {
  const el = document.getElementById('p2-ai-error-msg');
  if (!el) return;
  el.textContent = msg;
  el.style.display = 'block';
}

function _p2UpdateRunEnabled() {
  const btn = document.getElementById('btn-p2-ai-run');
  if (!btn) return;
  if (_p2IsPrivateManualBlocked()) {
    btn.disabled = true;
    return;
  }
  let ok = false;
  if (_p2InputMode === 'ai') {
    const sel = document.getElementById('p2-ai-report-select');
    const inp = document.getElementById('p2-pdf-input');
    const hasRep = sel && sel.value;
    const hasPdf = inp && inp.files && inp.files[0];
    ok = !!(hasRep || hasPdf);
  } else {
    const man = document.getElementById('p2-manual-product');
    ok = !!(man && man.value.trim());
  }
  btn.disabled = !ok;
}

function initP2Dropzone() {
  const z   = document.getElementById('p2-dropzone');
  const inp = document.getElementById('p2-pdf-input');
  if (!z || !inp) return;

  const onDrag = ev => { ev.preventDefault(); ev.stopPropagation(); };
  z.addEventListener('dragenter', onDrag);
  z.addEventListener('dragover', e => { onDrag(e); z.classList.add('p2-drag'); });
  z.addEventListener('dragleave', e => {
    onDrag(e);
    if (!z.contains(e.relatedTarget)) z.classList.remove('p2-drag');
  });
  z.addEventListener('drop', ev => {
    onDrag(ev);
    z.classList.remove('p2-drag');
    const f = ev.dataTransfer.files && ev.dataTransfer.files[0];
    if (!f) return;
    if (f.type !== 'application/pdf' && !String(f.name || '').toLowerCase().endsWith('.pdf')) {
      _p2ShowError('PDF 파일만 업로드할 수 있습니다.');
      return;
    }
    const dt = new DataTransfer();
    dt.items.add(f);
    inp.files = dt.files;
    _p2UpdatePdfLabel();
    _p2HideError();
    _p2UpdateRunEnabled();
  });

  inp.addEventListener('change', () => {
    _p2UpdatePdfLabel();
    _p2HideError();
    _p2UpdateRunEnabled();
  });
}

function _bindP2Inputs() {
  const sel = document.getElementById('p2-ai-report-select');
  if (sel) sel.addEventListener('change', () => { _p2HideError(); _p2UpdateRunEnabled(); });
  const man = document.getElementById('p2-manual-product');
  if (man) man.addEventListener('input', () => { _p2HideError(); _p2UpdateRunEnabled(); });
}

function _p2BuildRequestState() {
  if (_p2IsPrivateManualBlocked()) {
    throw new Error('민간 시장은 저장된 1공정 보고서 또는 PDF 경로만 지원합니다.');
  }

  if (_p2InputMode === 'ai') {
    const sel = document.getElementById('p2-ai-report-select');
    const inp = document.getElementById('p2-pdf-input');
    const reportId = sel && sel.value ? String(sel.value) : '';
    const pdfFile = inp && inp.files && inp.files[0] ? inp.files[0] : null;
    let reportData = null;

    if (reportId) {
      reportData = _loadReportFull(reportId);
      if (!reportData && !pdfFile) {
        throw new Error('선택한 저장 보고서에 전체 데이터가 없습니다. 1공정을 다시 실행하거나 PDF를 업로드하세요.');
      }
    }
    if (!reportData && !pdfFile) {
      throw new Error('저장된 1공정 보고서를 선택하거나 PDF를 업로드하세요.');
    }

    return {
      inputMode: 'ai',
      marketType: _p2Market,
      reportId: reportData ? reportId : '',
      reportData: reportData || null,
      pdfFile: reportData ? null : pdfFile,
      manualProduct: '',
    };
  }

  const man = document.getElementById('p2-manual-product');
  const manualProduct = man && man.value.trim();
  if (!manualProduct) {
    throw new Error('품목명을 입력하세요.');
  }
  return {
    inputMode: 'manual',
    marketType: _p2Market,
    reportId: '',
    reportData: null,
    pdfFile: null,
    manualProduct,
  };
}

function _p2BuildFormData(state, overrides = null) {
  const fd = new FormData();
  fd.append('input_mode', state.inputMode);
  fd.append('market_type', state.marketType);
  if (state.inputMode === 'ai') {
    if (state.reportId) fd.append('report_id', state.reportId);
    if (state.reportData) fd.append('report_data', JSON.stringify(state.reportData));
    if (!state.reportData && state.pdfFile) fd.append('pdf', state.pdfFile);
  } else {
    fd.append('manual_product', state.manualProduct);
  }
  if (overrides) fd.append('overrides', JSON.stringify(overrides));
  return fd;
}

function _p2FormatMoney(value, currency) {
  if (value == null || value === '') return '—';
  const digits = currency === 'KRW' ? 0 : 2;
  return `${currency} ${Number(value).toLocaleString('ko-KR', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

function _p2FormatPct(value, digits = 1) {
  if (value == null || value === '') return '—';
  return `${(Number(value) * 100).toFixed(digits)}%`;
}

function _p2RenderWarnings(items) {
  if (!items || !items.length) return '';
  return `
    <div class="p2-warnings">
      ${items.map(item => `<span class="p2-warning-chip">${_escHtml(item)}</span>`).join('')}
    </div>`;
}

// ── Phase 2: 가격 풀 출처 투명성 배너 ───────────────────────────────────
// price_pool_sources: [{source, tier, origin, count}]
// price_pool_tier_counts: {public, procurement, retail, self_or_estimated, unknown}
// diversity_warnings: [str, ...]
function _p2RenderPriceSources(data) {
  const sources = Array.isArray(data.price_pool_sources) ? data.price_pool_sources : [];
  const tierCounts = data.price_pool_tier_counts || {};
  const divWarnings = Array.isArray(data.diversity_warnings) ? data.diversity_warnings : [];
  if (!sources.length && !divWarnings.length) return '';

  const total = sources.reduce((a, s) => a + (Number(s.count) || 0), 0) || 0;
  const pub = Number(tierCounts.public || 0);
  const proc = Number(tierCounts.procurement || 0);
  const ret = Number(tierCounts.retail || 0);
  const self = Number(tierCounts.self_or_estimated || 0);
  const unk = Number(tierCounts.unknown || 0);

  const pct = (n) => (total > 0 ? Math.max(2, Math.round((n / total) * 100)) : 0);
  const bar = (total > 0) ? `
    <div class="p2-tier-bar" title="출처 tier 분포">
      ${pub > 0 ? `<span class="p2-tier-seg p2-tier-public" style="flex:${pub};" title="공공/SFDA: ${pub}건">${pct(pub)}%</span>` : ''}
      ${proc > 0 ? `<span class="p2-tier-seg p2-tier-proc" style="flex:${proc};" title="조달/NUPCO·Etimad: ${proc}건">${pct(proc)}%</span>` : ''}
      ${ret > 0 ? `<span class="p2-tier-seg p2-tier-retail" style="flex:${ret};" title="민간 소매: ${ret}건">${pct(ret)}%</span>` : ''}
      ${self > 0 ? `<span class="p2-tier-seg p2-tier-self" style="flex:${self};" title="자체/추정: ${self}건">${pct(self)}%</span>` : ''}
      ${unk > 0 ? `<span class="p2-tier-seg p2-tier-unknown" style="flex:${unk};" title="미분류: ${unk}건">${pct(unk)}%</span>` : ''}
    </div>` : '';

  const chips = sources.slice(0, 10).map(s => {
    const originCls = ({
      public: 'p2-src-public',
      procurement: 'p2-src-proc',
      retail: 'p2-src-retail',
      self: 'p2-src-self',
      estimated: 'p2-src-self',
    })[s.origin] || 'p2-src-unknown';
    return `<span class="p2-src-chip ${originCls}">${_escHtml(s.source)} · ${Number(s.count || 0)}건</span>`;
  }).join('');

  const warns = divWarnings.length ? `
    <div class="p2-diversity-warn">
      ${divWarnings.map(w => `<div class="p2-diversity-warn-item">${_escHtml(w)}</div>`).join('')}
    </div>` : '';

  const legend = `
    <div class="p2-tier-legend">
      ${pub > 0 ? `<span><span class="dot p2-tier-public"></span>공공 ${pub}</span>` : ''}
      ${proc > 0 ? `<span><span class="dot p2-tier-proc"></span>조달 ${proc}</span>` : ''}
      ${ret > 0 ? `<span><span class="dot p2-tier-retail"></span>민간 소매 ${ret}</span>` : ''}
      ${self > 0 ? `<span><span class="dot p2-tier-self"></span>자체/추정 ${self}</span>` : ''}
      ${unk > 0 ? `<span><span class="dot p2-tier-unknown"></span>미분류 ${unk}</span>` : ''}
    </div>`;

  return `
    <div class="p2-sources-panel">
      <div class="p2-sources-title">가격 풀 출처 구성 <span class="p2-sources-total">총 ${total}건</span></div>
      ${bar}
      ${legend}
      ${chips ? `<div class="p2-src-chips">${chips}</div>` : ''}
      ${warns}
    </div>`;
}

function _p2RenderStepsTable(steps) {
  if (!steps || !steps.length) return '';
  return `
    <div class="p2-steps-table-wrap">
      <table class="p2-steps-table">
        <thead>
          <tr><th>단계</th><th>SAR</th></tr>
        </thead>
        <tbody>
          ${steps.map(step => `
            <tr>
              <td>${_escHtml(step.label || '')}</td>
              <td>${_p2FormatMoney(step.value_sar, 'SAR')}</td>
            </tr>`).join('')}
        </tbody>
      </table>
    </div>`;
}

function _p2CollectColumnOverrides() {
  const map = { agg: 'aggressive', avg: 'average', cons: 'conservative' };
  const scenarios = {};
  for (const [col, name] of Object.entries(map)) {
    const base = document.getElementById(`p2ci-base-${col}`);
    const fee = document.getElementById(`p2ci-fee-${col}`);
    const fr = document.getElementById(`p2ci-freight-${col}`);
    if (!fee || !fr) continue;
    const entry = {
      agent_commission_pct: Number(fee.value) / 100,
      freight_multiplier: Number(fr.value),
    };
    const rb = base ? Number(base.value) : NaN;
    if (!Number.isNaN(rb) && rb > 0) {
      entry.retail_base = rb;
    }
    scenarios[name] = entry;
  }
  return { scenarios };
}

function toggleP2ColDetail(col) {
  const detail = document.getElementById(`p2cd-${col}`);
  const row = detail?.previousElementSibling;
  const btn = row?.querySelector('.p2-col-expand-btn');
  if (!detail) return;
  const isHidden = detail.style.display === 'none';
  detail.style.display = isHidden ? 'block' : 'none';
  if (btn) {
    btn.textContent = `${isHidden ? '▾' : '▸'} 역산 · 옵션 편집`;
  }
}

function recalcP2Col(col) {
  if (_p2Market !== 'private' || !_p2LastRequestState) return;
  if (_p2OverrideTimer) clearTimeout(_p2OverrideTimer);
  _p2OverrideTimer = setTimeout(() => {
    _submitP2Analysis({ useLastState: true, overrides: _p2CollectColumnOverrides() });
  }, 400);
}

function renderP2Result(data) {
  const sec = document.getElementById('p2-ai-result-section');
  if (!sec) return;

  const stats = data.competitor_stats || {};
  const product = data.product || {};
  const classification = data.classification || {};
  const scenarios = data.scenarios || {};
  const notes = data.notes || [];
  const reg = data.regulatory_cost || {};
  const pairs = [
    { key: 'aggressive', col: 'agg' },
    { key: 'average', col: 'avg' },
    { key: 'conservative', col: 'cons' },
  ];

  for (const { key, col } of pairs) {
    const sc = scenarios[key] || {};
    const priceEl = document.getElementById(`p2c-price-${col}`);
    const subEl = document.getElementById(`p2c-sub-${col}`);
    const baseInp = document.getElementById(`p2ci-base-${col}`);
    const feeInp = document.getElementById(`p2ci-fee-${col}`);
    const frInp = document.getElementById(`p2ci-freight-${col}`);
    const stepsBody = document.getElementById(`p2-steps-body-${col}`);

    if (sc.error) {
      if (priceEl) priceEl.textContent = '—';
      if (subEl) subEl.textContent = String(sc.error);
    } else {
      if (priceEl) {
        priceEl.textContent = sc.fob_sar != null
          ? Number(sc.fob_sar).toLocaleString('ko-KR', {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2,
          })
          : '—';
      }
      if (subEl) {
        const u = sc.fob_usd != null
          ? Number(sc.fob_usd).toLocaleString('en-US', {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2,
          })
          : '—';
        const k = sc.fob_krw != null
          ? Number(sc.fob_krw).toLocaleString('ko-KR', { maximumFractionDigits: 0 })
          : '—';
        subEl.textContent = `${u} USD · ${k} KRW`;
      }
      if (baseInp && sc.retail_sar != null) baseInp.value = String(sc.retail_sar);
      if (feeInp && sc.agent_commission_pct != null) {
        feeInp.value = String((Number(sc.agent_commission_pct) * 100).toFixed(1));
      }
      if (frInp && sc.freight_multiplier != null) {
        frInp.value = String(Number(sc.freight_multiplier).toFixed(2));
      }
    }
    if (stepsBody) {
      stepsBody.innerHTML = _p2RenderStepsTable(sc.steps || []);
    }
  }

  const prodList = document.getElementById('p2-product-list');
  if (prodList) {
    const title = _escHtml(product.trade_name || '민간 시장 FOB 분석');
    const sub = _escHtml([product.inn, product.strength, product.dosage_form].filter(Boolean).join(' · '));
    const ratFull = String(classification.rationale || '').trim();
    const ratShort = ratFull.length > 320 ? `${ratFull.slice(0, 320)}…` : ratFull;
    const tagParts = [
      classification.product_kind ? `분류 ${classification.product_kind}` : '',
      classification.is_combination ? '복합제' : '단일 성분',
      classification.is_extended_release ? '서방형/개량신약' : '표준 제형',
    ].filter(Boolean);

    const distParts = ['min', 'p25', 'median', 'p75', 'max', 'avg'].map(k => {
      const v = stats[k];
      if (v == null || v === '') return '';
      return `<span>${k}: ${_p2FormatMoney(v, 'SAR')}</span>`;
    }).filter(Boolean);

    let html = `
      <div class="p2-result-meta">
        <div class="p2-result-meta-title">${title}</div>
        <div>${sub}</div>
        ${tagParts.length ? `<div style="margin-top:8px;font-size:12px;color:var(--muted);">${_escHtml(tagParts.join(' · '))}</div>` : ''}
        ${ratShort ? `<div style="margin-top:10px;line-height:1.6;">${_escHtml(ratShort)}</div>` : ''}
        ${_p2RenderWarnings(classification.warnings || [])}
        ${_p2RenderPriceSources(data)}
        ${distParts.length ? `<div class="p2-dist-inline">${distParts.join('')}</div>` : ''}
        ${stats.warning ? `<div style="margin-top:8px;font-size:12px;color:var(--orange2);font-weight:700;">${_escHtml(stats.warning)}</div>` : ''}
      </div>`;

    html += `
      <table class="p2-prod-table">
        <thead><tr><th>제품</th><th>식별</th><th>참고</th></tr></thead>
        <tbody>
          <tr>
            <td>${_escHtml(product.trade_name || '—')}</td>
            <td>${_escHtml([product.inn, product.strength].filter(Boolean).join(' · ') || '—')}</td>
            <td>${_escHtml(product.hs_code ? `HS ${product.hs_code}` : '—')}</td>
          </tr>
        </tbody>
      </table>`;

    const perUnit = reg.per_unit_amortization_sar != null
      ? _p2FormatMoney(reg.per_unit_amortization_sar, 'SAR')
      : '—';
    html += `
      <div class="p2-reg-cost" style="margin-top:14px;padding:12px 14px;border-radius:14px;background:var(--inner);">
        <div style="display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap;">
          <span style="font-weight:900;color:var(--navy);">규제비 감가상각</span>
          <span style="font-weight:800;color:var(--navy);">${perUnit} / unit</span>
        </div>
        <div style="margin-top:8px;font-size:12px;line-height:1.7;color:var(--muted);">
          SFDA ${_p2FormatMoney(reg.sfda_registration_sar, 'SAR')}
          · SABER PCoC ${_p2FormatMoney(reg.saber_pcoc_annual_sar, 'SAR')}
          · SCoC(연) ${_p2FormatMoney(reg.saber_scoc_annual_sar, 'SAR')}
          · 연간 수량 ${Number(reg.assumptions?.annual_units || 0).toLocaleString('ko-KR')}
        </div>
      </div>`;

    if (notes.length) {
      html += `<div class="p2-notes" style="margin-top:12px;">${notes.map(n => `<div class="p2-note-item">${_escHtml(n)}</div>`).join('')}</div>`;
    }

    prodList.innerHTML = html;
  }

  const dl = document.getElementById('p2-report-dl-state');
  if (dl) {
    const pdfName = data.pdf ? String(data.pdf) : '';
    if (pdfName) {
      dl.innerHTML = `<a class="btn-download" href="/api/report/download?name=${encodeURIComponent(pdfName)}" target="_blank" rel="noopener noreferrer">📄 수출가격전략 보고서 다운로드</a>`;
    } else {
      dl.innerHTML = '<span style="font-size:12px;color:var(--muted);">생성된 P2 PDF가 없습니다.</span>';
    }
  }

  sec.style.display = '';
}

async function _submitP2Analysis({ useLastState = false, overrides = null } = {}) {
  _p2HideError();
  _p2ResetResultView(false);

  let state;
  try {
    state = useLastState
      ? (_p2LastRequestState ? { ..._p2LastRequestState } : null)
      : _p2BuildRequestState();
    if (!state) throw new Error('이전 요청 상태가 없어 오버라이드 재계산을 할 수 없습니다.');
  } catch (e) {
    _p2ShowError(e.message || '2공정 요청 구성을 만들지 못했습니다.');
    return;
  }

  await _p2PerformRequest(state, overrides);
}

async function _p2PerformRequest(state, overrides = null) {
  if (_p2Running) return;
  _p2Running = true;
  const btn  = document.getElementById('btn-p2-ai-run');
  const icon = document.getElementById('p2-ai-run-icon');
  if (btn) btn.disabled = true;
  if (icon) icon.textContent = '⏳';
  _showP2Loading();

  try {
    const fd = _p2BuildFormData(state, overrides);
    const res = await fetch('/api/p2/price-analyze', { method: 'POST', body: fd });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.ok === false) {
      _p2ShowError(data.detail || data.message || `요청 실패 (${res.status})`);
      return;
    }

    _p2LastRequestState = state;
    if (data.ok && data.scenarios) {
      renderP2Result(data);
      return;
    }

    const sec = document.getElementById('p2-ai-result-section');
    const list = document.getElementById('p2-product-list');
    const dlStub = document.getElementById('p2-report-dl-state');
    if (data.ok && !data.scenarios && sec && list) {
      const msg = data.message || '처리되었습니다.';
      sec.style.display = '';
      list.innerHTML = `<p class="p2-stub-msg">${_escHtml(msg)}</p>`;
      if (dlStub) dlStub.innerHTML = '';
      return;
    }

    _p2ShowError(data.detail || data.message || '응답을 해석할 수 없습니다.');
  } catch (e) {
    console.warn('2공정 분석 요청 실패:', e);
    _p2ShowError('네트워크 오류 — 잠시 후 다시 시도해 주세요.');
  } finally {
    _p2Running = false;
    if (icon) icon.textContent = '▶';
    _hideP2Loading();
    _p2UpdateRunEnabled();
  }
}

async function runP2PriceAnalysis() {
  return _submitP2Analysis();
}

/* ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   §12b. 3공정 · 바이어 발굴 (Perplexity prospects)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */

const P3_LS_KEY = 'sa_upharma_p3_prospects_v1';

function _p3LoadStore() {
  try   { return JSON.parse(localStorage.getItem(P3_LS_KEY) || '[]'); }
  catch { return []; }
}

function _p3SaveStore(items) {
  localStorage.setItem(P3_LS_KEY, JSON.stringify(items || []));
}

function _p3HideError() {
  const el = document.getElementById('p3-inline-error');
  if (el) { el.style.display = 'none'; el.textContent = ''; }
}

function _p3ShowError(msg) {
  const el = document.getElementById('p3-inline-error');
  if (!el) return;
  el.textContent = msg || '요청 실패';
  el.style.display = 'block';
}

function _p3SetLoading(loading) {
  const btn = document.getElementById('p3-btn-run');
  const icon = document.getElementById('p3-btn-icon');
  if (btn) btn.disabled = !!loading;
  if (icon) icon.textContent = loading ? '⏳' : '▶';
}

function _p3RenderResults(payload) {
  const host = document.getElementById('p3-results');
  if (!host) return;

  const items = (payload && payload.items) ? payload.items : [];
  if (!items.length) {
    host.innerHTML = '<div class="p3-empty">후보를 찾지 못했습니다. 키워드를 바꿔 다시 시도해 주세요.</div>';
    host.style.display = 'block';
    return;
  }

  host.innerHTML = items.map(it => {
    const url = String(it.url || '');
    const safeUrl = /^https?:\/\//.test(url) ? url : '#';
    const score = (it.relevance_score != null) ? Number(it.relevance_score) : null;
    const scoreText = (score != null && !Number.isNaN(score)) ? score.toFixed(2) : '—';
    const cat = String(it.category || 'other');
    const lang = String(it.language || '');
    const title = String(it.title || it.domain || url || '');
    const desc = String(it.description || '');
    const tags = [
      cat ? `<span class="p3-tag">${_escHtml(cat)}</span>` : '',
      lang ? `<span class="p3-tag gray">${_escHtml(lang)}</span>` : '',
      `<span class="p3-tag gray">score ${_escHtml(scoreText)}</span>`,
      it.has_price_data ? `<span class="p3-tag green">price</span>` : '',
      it.has_product_listing ? `<span class="p3-tag blue">listing</span>` : '',
    ].filter(Boolean).join('');

    return `
      <article class="p3-item">
        <div class="p3-item-head">
          <div class="p3-item-title">${_escHtml(title)}</div>
          <a class="btn-download p3-open" href="${_escHtml(safeUrl)}" target="_blank" rel="noopener noreferrer">열기</a>
        </div>
        <div class="p3-item-url">${_escHtml(it.domain || '')}</div>
        ${desc ? `<div class="p3-item-desc">${_escHtml(desc)}</div>` : ''}
        <div class="p3-tags">${tags}</div>
      </article>
    `;
  }).join('');

  host.style.display = 'block';
}

function _p3RenderCache() {
  const box = document.getElementById('p3-cache');
  const list = document.getElementById('p3-cache-list');
  if (!box || !list) return;

  const store = _p3LoadStore();
  if (!store.length) {
    box.style.display = 'none';
    list.innerHTML = '';
    return;
  }

  box.style.display = 'block';
  list.innerHTML = store.map(entry => {
    const label = String(entry.label || '바이어 후보');
    const ts = String(entry.timestamp || '');
    const count = Number(entry.count || 0);
    return `
      <button type="button" class="p3-cache-item" onclick="_p3UseCache(${Number(entry.id)})">
        <div class="p3-cache-item-title">${_escHtml(label)}</div>
        <div class="p3-cache-item-meta">${_escHtml(ts)} · ${_escHtml(String(count))}건</div>
      </button>
    `;
  }).join('');
}

function _p3UseCache(id) {
  const store = _p3LoadStore();
  const found = store.find(x => Number(x.id) === Number(id));
  if (!found) return;
  _p3HideError();
  _p3RenderResults({ items: found.items || [] });
}

async function runP3Prospects() {
  _p3HideError();
  _p3SetLoading(true);

  const sel = document.getElementById('p3-product-select');
  const kw = document.getElementById('p3-keyword');
  const productKey = sel && sel.value ? String(sel.value) : '';
  const keyword = kw && kw.value ? String(kw.value).trim() : '';

  const payload = productKey
    ? { product_key: productKey }
    : { trade_name: keyword };

  if (!payload.product_key && !payload.trade_name) {
    _p3ShowError('품목을 선택하거나 키워드를 입력하세요.');
    _p3SetLoading(false);
    return;
  }

  try {
    const res = await fetch('/api/p3/prospects', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) {
      const msg = data.error || data.detail || `요청 실패 (${res.status})`;
      _p3ShowError(msg);
      return;
    }

    _p3RenderResults(data);

    const now = Date.now();
    const entry = {
      id: now,
      label: productKey ? `품목: ${productKey}` : `키워드: ${keyword}`,
      timestamp: new Date().toLocaleString('ko-KR', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }),
      count: Number(data.count || 0),
      items: data.items || [],
    };
    const store = _p3LoadStore();
    store.unshift(entry);
    _p3SaveStore(store.slice(0, 10));
    _p3RenderCache();
  } catch (e) {
    console.warn('P3 prospects 요청 실패:', e);
    _p3ShowError('네트워크 오류 — 잠시 후 다시 시도해 주세요.');
  } finally {
    _p3SetLoading(false);
  }
}

/* ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   §12b. Phase 3 — White-Space (빈틈) 포트폴리오 분석
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */

function _p3WSHideError() {
  const el = document.getElementById('p3-ws-error');
  if (el) { el.style.display = 'none'; el.textContent = ''; }
}

function _p3WSShowError(msg) {
  const el = document.getElementById('p3-ws-error');
  if (!el) return;
  el.textContent = msg || '요청 실패';
  el.style.display = 'block';
}

function _p3WSSetLoading(loading) {
  const btn = document.getElementById('p3-ws-btn');
  const icon = document.getElementById('p3-ws-btn-icon');
  if (btn) btn.disabled = !!loading;
  if (icon) icon.textContent = loading ? '⏳' : '🔍';
}

function _p3WSStrengthClass(score) {
  const s = Number(score) || 0;
  if (s >= 70) return 'strong';
  if (s >= 40) return 'mid';
  return 'weak';
}

/** Phase 4: Tender Power 셀 HTML. band: strong/mid/weak/none */
function _p3WSRenderTenderPower(tp) {
  if (!tp) return '';
  const score   = Number(tp.score || 0);
  const band    = String(tp.band || 'none');
  const count   = Number(tp.count_last_2y || 0);
  const mnSar   = Number(tp.total_value_mn_sar || 0);
  const atcMatch = !!tp.has_target_atc_match;
  const sources = tp.sources || {};
  const etimadN = Number(sources.etimad || 0);
  const nupcoN  = Number(sources.nupco  || 0);

  if (score <= 0 && count === 0) {
    return `
      <div class="p3-ws-tp none">
        <div class="p3-ws-tp-label">Tender Power</div>
        <div class="p3-ws-tp-val muted">실적 없음</div>
      </div>
    `;
  }

  const valStr = mnSar >= 1
    ? `${mnSar.toFixed(1)}M SAR`
    : `${Math.round(mnSar * 1000)}K SAR`;
  const atcBadge = atcMatch
    ? `<span class="p3-ws-tp-atc">✓ 타겟 ATC 낙찰</span>`
    : '';
  const srcChips = [
    etimadN > 0 ? `<span class="p3-ws-tp-src etimad">Etimad ${etimadN}</span>` : '',
    nupcoN  > 0 ? `<span class="p3-ws-tp-src nupco">NUPCO ${nupcoN}</span>`   : '',
  ].filter(Boolean).join('');

  return `
    <div class="p3-ws-tp ${_escHtml(band)}">
      <div class="p3-ws-tp-main">
        <div class="p3-ws-tp-label">Tender Power</div>
        <div class="p3-ws-tp-val">
          <span class="p3-ws-tp-score">${score.toFixed(1)}</span>
          <span class="p3-ws-tp-unit">/100</span>
        </div>
      </div>
      <div class="p3-ws-tp-stats">
        <span>2년 ${count}건</span>
        <span>·</span>
        <span>${_escHtml(valStr)}</span>
        ${atcBadge ? ' · ' + atcBadge : ''}
      </div>
      ${srcChips ? `<div class="p3-ws-tp-sources">${srcChips}</div>` : ''}
    </div>
  `;
}

function _p3WSRenderSummary(data) {
  const host = document.getElementById('p3-ws-summary');
  if (!host) return;

  const innLabel  = String(data.target_inn || '-');
  const atcLabel  = String(data.target_atc_level3 || '-');
  const totalAgt  = Number(data.total_agents || 0);
  const inAtc     = Number(data.agents_in_atc || 0);
  const scanned   = Number(data.products_scanned || 0);
  const unmatched = Number(data.unmatched_product_count || 0);
  const cand      = Array.isArray(data.candidates) ? data.candidates.length : 0;

  const noteHtml = data.error
    ? `<div class="p3-ws-note warn">⚠️ ${_escHtml(String(data.error))}</div>`
    : '';

  // Phase 4: Tender Power 메타 (공공조달 실적 커버리지)
  const tpMeta = data.tender_power_meta || null;
  let tpMetaHtml = '';
  if (tpMeta) {
    if (tpMeta.error) {
      tpMetaHtml = `<div class="p3-ws-note warn">Tender Power 계산 실패: ${_escHtml(tpMeta.error)}</div>`;
    } else if (tpMeta.note) {
      tpMetaHtml = `<div class="p3-ws-note info">ℹ️ ${_escHtml(tpMeta.note)}</div>`;
    } else {
      const cN  = Number(tpMeta.contracts_scanned || 0);
      const aN  = Number(tpMeta.awards_scanned || 0);
      const um  = Number(tpMeta.unmatched_supplier_count || 0);
      const sortLabel = tpMeta.sort_applied === 'tender_power_desc'
        ? '정렬: Tender Power 내림차순'
        : '';
      tpMetaHtml = `
        <div class="p3-ws-note info">
          📊 공공조달 실적 스캔: Etimad ${cN}건 · NUPCO ${aN}건
          ${um > 0 ? `· 미매치 공급자 ${um}건` : ''}
          ${sortLabel ? `· ${_escHtml(sortLabel)}` : ''}
        </div>
      `;
    }
  }

  host.innerHTML = `
    <div class="p3-ws-summary-row">
      <div class="p3-ws-stat">
        <div class="p3-ws-stat-lbl">타겟 INN</div>
        <div class="p3-ws-stat-val">${_escHtml(innLabel)}</div>
      </div>
      <div class="p3-ws-stat">
        <div class="p3-ws-stat-lbl">ATC level3</div>
        <div class="p3-ws-stat-val">${_escHtml(atcLabel)}</div>
      </div>
      <div class="p3-ws-stat">
        <div class="p3-ws-stat-lbl">전체 에이전트</div>
        <div class="p3-ws-stat-val">${totalAgt}</div>
      </div>
      <div class="p3-ws-stat">
        <div class="p3-ws-stat-lbl">치료군 내 강자</div>
        <div class="p3-ws-stat-val">${inAtc}</div>
      </div>
      <div class="p3-ws-stat">
        <div class="p3-ws-stat-lbl">빈틈 후보</div>
        <div class="p3-ws-stat-val accent">${cand}</div>
      </div>
      <div class="p3-ws-stat">
        <div class="p3-ws-stat-lbl">스캔 품목</div>
        <div class="p3-ws-stat-val">${scanned}${unmatched ? ` <span class="p3-ws-unmatched">(${unmatched} 미매치)</span>` : ''}</div>
      </div>
    </div>
    ${tpMetaHtml}
    ${noteHtml}
  `;
  host.style.display = 'block';
}

function _p3WSRenderResults(data) {
  const host = document.getElementById('p3-ws-results');
  if (!host) return;

  const candidates = Array.isArray(data.candidates) ? data.candidates : [];
  if (!candidates.length) {
    host.innerHTML = `<div class="p3-empty">
      빈틈 후보가 없습니다. ATC level3 또는 최소 품목 수를 조정하거나,
      타겟 INN 을 다른 철자로 시도해 보세요.
    </div>`;
    host.style.display = 'block';
    return;
  }

  const rows = candidates.map((c, idx) => {
    const agent   = String(c.agent_name || c.normalized_name || '?');
    const atc     = String(c.atc_level3 || '-');
    const inAtc   = Number(c.product_count_in_atc || 0);
    const tot     = Number(c.total_products || 0);
    const strg    = Number(c.portfolio_strength || 0);
    const miss    = !!c.missing_ingredient;
    const pitch   = String(c.sales_pitch || '');
    const samples = Array.isArray(c.sample_trade_names) ? c.sample_trade_names.slice(0, 5) : [];
    const strgCls = _p3WSStrengthClass(strg);
    const missBadge = miss
      ? `<span class="p3-ws-badge missing">🎯 타겟 미취급</span>`
      : `<span class="p3-ws-badge carrying">이미 취급 중</span>`;

    // Phase 4: Tender Power
    const tp = c.tender_power || null;
    const tpHtml = tp ? _p3WSRenderTenderPower(tp) : '';

    const samplesHtml = samples.length
      ? `<div class="p3-ws-samples">
           ${samples.map(s => `<span class="p3-ws-sample">${_escHtml(String(s))}</span>`).join('')}
         </div>`
      : '';

    return `
      <article class="p3-ws-item">
        <div class="p3-ws-item-head">
          <div class="p3-ws-rank">#${idx + 1}</div>
          <div class="p3-ws-item-main">
            <div class="p3-ws-item-agent">${_escHtml(agent)}</div>
            <div class="p3-ws-item-meta">
              <span>ATC ${_escHtml(atc)}</span>
              <span>·</span>
              <span>치료군 ${inAtc}품목</span>
              <span>·</span>
              <span>전체 ${tot}품목</span>
            </div>
          </div>
          <div class="p3-ws-strength ${strgCls}">
            <div class="p3-ws-strength-val">${strg.toFixed(1)}</div>
            <div class="p3-ws-strength-lbl">strength</div>
          </div>
          ${missBadge}
        </div>
        ${tpHtml}
        <div class="p3-ws-pitch">💡 ${_escHtml(pitch)}</div>
        ${samplesHtml}
      </article>
    `;
  }).join('');

  host.innerHTML = rows;
  host.style.display = 'block';
}

async function runP3WhiteSpace() {
  _p3WSHideError();

  const innEl = document.getElementById('p3-ws-inn');
  const atcEl = document.getElementById('p3-ws-atc');
  const minEl = document.getElementById('p3-ws-min');

  const targetInn = innEl && innEl.value ? String(innEl.value).trim() : '';
  const targetAtc = atcEl && atcEl.value ? String(atcEl.value).trim().toUpperCase() : '';
  const minProd   = minEl && minEl.value ? Number(minEl.value) : 3;

  if (!targetInn && !targetAtc) {
    _p3WSShowError('타겟 INN 또는 ATC4 중 하나는 입력하세요.');
    return;
  }
  if (Number.isNaN(minProd) || minProd < 1) {
    _p3WSShowError('최소 품목 수는 1 이상의 숫자여야 합니다.');
    return;
  }

  _p3WSSetLoading(true);

  try {
    const res = await fetch('/api/p3/white-space', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        target_inn: targetInn,
        target_atc_level3: targetAtc || null,
        min_atc_products: minProd,
        top_n: 20,
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) {
      const msg = data.error || data.detail || `요청 실패 (${res.status})`;
      _p3WSShowError(msg);
      const sum = document.getElementById('p3-ws-summary');
      const rst = document.getElementById('p3-ws-results');
      if (sum) sum.style.display = 'none';
      if (rst) rst.style.display = 'none';
      return;
    }

    _p3WSRenderSummary(data);
    _p3WSRenderResults(data);
  } catch (e) {
    console.warn('P3 white-space 요청 실패:', e);
    _p3WSShowError('네트워크 오류 — 잠시 후 다시 시도해 주세요.');
  } finally {
    _p3WSSetLoading(false);
  }
}

/* ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   §12c. Phase 5 — 경쟁사 유통 에이전트 역추적
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */

function _cmShowStatus(msg, cls) {
  const el = document.getElementById('cm-status');
  if (!el) return;
  el.textContent = String(msg || '');
  el.className = 'cm-status' + (cls ? ' ' + cls : '');
  el.style.display = msg ? 'block' : 'none';
}

function _cmRenderSummary(data) {
  const host = document.getElementById('cm-summary');
  if (!host) return;
  const total = Number(data.total_agents || 0);
  const brands = Number(data.total_brands || 0);
  const shown = (data.agents || []).length;
  const scanned = Number(data.products_scanned || 0);
  const matched = Number(data.products_matched || 0);
  const tenderN = Number(data.tender_records_used || 0);
  const f = data.filters || {};
  const atc = f.target_atc_l3 || '-';
  const tokens = Array.isArray(f.inn_tokens) ? f.inn_tokens.join(', ') : '';

  host.innerHTML = `
    <div class="cm-summary-row">
      <div class="cm-stat"><div class="cm-stat-lbl">경쟁 에이전트</div><div class="cm-stat-val">${total}</div></div>
      <div class="cm-stat"><div class="cm-stat-lbl">경쟁 브랜드</div><div class="cm-stat-val">${brands}</div></div>
      <div class="cm-stat"><div class="cm-stat-lbl">표시 중</div><div class="cm-stat-val accent">${shown}</div></div>
      <div class="cm-stat"><div class="cm-stat-lbl">ATC L3</div><div class="cm-stat-val">${_escHtml(atc)}</div></div>
      <div class="cm-stat"><div class="cm-stat-lbl">매칭/스캔</div><div class="cm-stat-val">${matched}/${scanned}</div></div>
      ${tenderN ? `<div class="cm-stat"><div class="cm-stat-lbl">Tender 실적</div><div class="cm-stat-val">${tenderN}건</div></div>` : ''}
    </div>
    ${tokens ? `<div class="cm-filter-note">성분 토큰: ${_escHtml(tokens)}</div>` : ''}
  `;
  host.style.display = 'block';
}

function _cmShareClass(share) {
  const s = Number(share) || 0;
  if (s >= 0.30) return 'top';
  if (s >= 0.15) return 'mid';
  return 'low';
}

function _cmRenderList(data) {
  const host = document.getElementById('cm-list');
  if (!host) return;

  const agents = Array.isArray(data.agents) ? data.agents : [];
  if (!agents.length) {
    host.innerHTML = `<div class="cm-empty">경쟁 브랜드 매칭이 없습니다. 성분/ATC 필터를 조정해 보세요.</div>`;
    host.style.display = 'block';
    return;
  }

  const items = agents.map((a, idx) => {
    const name = String(a.agent_name || a.normalized_name || '?');
    const brands = Array.isArray(a.competitor_brands) ? a.competitor_brands : [];
    const brandCount = Number(a.brand_count || brands.length || 0);
    const share = Number(a.market_share_est || 0);
    const sharePct = (share * 100).toFixed(1);
    const tenderN = Number(a.tender_count || 0);
    const tenderM = Number(a.tender_total_mn_sar || 0);
    const avg = a.avg_price_sar != null ? Number(a.avg_price_sar).toFixed(1) + ' SAR' : '—';
    const range = (a.min_price_sar != null && a.max_price_sar != null)
      ? `${Number(a.min_price_sar).toFixed(1)}–${Number(a.max_price_sar).toFixed(1)}`
      : '';
    const shareCls = _cmShareClass(share);

    const brandChips = brands.slice(0, 8).map(b => {
      const bn = String(b.trade_name || '?');
      const price = b.price_sar != null ? ` · ${Number(b.price_sar).toFixed(1)}` : '';
      return `<span class="cm-brand-chip">${_escHtml(bn)}${_escHtml(price)}</span>`;
    }).join('');
    const moreChip = brands.length > 8 ? `<span class="cm-brand-more">+${brands.length - 8}</span>` : '';

    const tenderStr = tenderN > 0
      ? `<span class="cm-tender-chip">📊 Tender ${tenderN}건 · ${tenderM.toFixed(1)}M SAR</span>`
      : '';

    return `
      <article class="cm-item ${shareCls}">
        <div class="cm-item-head">
          <div class="cm-rank">#${idx + 1}</div>
          <div class="cm-item-main">
            <div class="cm-item-agent">${_escHtml(name)}</div>
            <div class="cm-item-meta">
              <span>${brandCount}개 브랜드</span>
              <span>·</span>
              <span>평균가 ${_escHtml(avg)}${range ? ` (range ${_escHtml(range)})` : ''}</span>
              ${tenderStr ? '· ' + tenderStr : ''}
            </div>
          </div>
          <div class="cm-share">
            <div class="cm-share-bar"><div class="cm-share-fill" style="width:${Math.min(100, share * 100).toFixed(1)}%"></div></div>
            <div class="cm-share-val">${sharePct}%</div>
          </div>
        </div>
        <div class="cm-brands">${brandChips}${moreChip}</div>
      </article>
    `;
  }).join('');

  host.innerHTML = items;
  host.style.display = 'block';
}

async function loadCompetitorMap(opts) {
  const card = document.getElementById('competitor-map-card');
  if (!card) return;

  const body = {
    product_key: opts?.product_key || null,
    trade_name: opts?.trade_name || null,
    target_inn: opts?.target_inn || null,
    target_atc_level3: opts?.target_atc_level3 || null,
    include_tender_power: true,
    top_n: 15,
  };

  // 최소 1개 필터라도 있어야 호출
  if (!body.product_key && !body.trade_name && !body.target_inn) {
    return;
  }

  card.style.display = 'block';
  _cmShowStatus('경쟁사 유통 구도 분석 중…', 'loading');
  const sum = document.getElementById('cm-summary');
  const lst = document.getElementById('cm-list');
  if (sum) sum.style.display = 'none';
  if (lst) lst.style.display = 'none';

  try {
    const res = await fetch('/api/p1/competitor-map', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) {
      _cmShowStatus(data.error || `경쟁사 맵 로드 실패 (${res.status})`, 'err');
      return;
    }
    if (Array.isArray(data.notes) && data.notes.length) {
      _cmShowStatus(data.notes.join(' · '), 'info');
    } else {
      _cmShowStatus('', '');
    }
    _cmRenderSummary(data);
    _cmRenderList(data);
  } catch (e) {
    console.warn('competitor-map 요청 실패:', e);
    _cmShowStatus('네트워크 오류 — 잠시 후 다시 시도해 주세요.', 'err');
  }
}

/* ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   §SA-P3. 바이어 발굴 파이프라인 (Singapore 방식 래퍼)
   POST /api/buyers/run → poll /api/buyers/status → GET /api/buyers/result
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */

let _p3PollTimer        = null;
let _p3Buyers           = [];
let _p3DisplayedBuyers  = [];
let _p3PdfName          = null;
let _p3SelectedReportId = '';

function _syncP3ReportOptions() {
  const sel = document.getElementById('p3-report-select');
  if (!sel) return;
  const p1Reports = _loadReports().filter(r => !r.report_type || r.report_type === 'p1');
  sel.innerHTML = ['<option value="">시장조사 보고서를 선택하세요</option>']
    .concat(p1Reports.map(r => {
      const name = r.product || r.report_title || '보고서';
      return `<option value="${r.id}">시장조사 보고서 · ${_escHtml(name)} · ${_escHtml(r.timestamp || '')}</option>`;
    })).join('');

  const noReportBanner = document.getElementById('p3-no-report-banner');
  if (p1Reports.length) {
    if (noReportBanner) noReportBanner.style.display = 'none';
    if (!_p3SelectedReportId || !p1Reports.find(r => String(r.id) === _p3SelectedReportId)) {
      _p3SelectedReportId = String(p1Reports[0].id);
    }
    sel.value = _p3SelectedReportId;
  } else {
    if (noReportBanner) noReportBanner.style.display = '';
  }
}

function onP3ReportChange() {
  const sel = document.getElementById('p3-report-select');
  _p3SelectedReportId = sel?.value || '';
}

async function runP3Pipeline() {
  const btn     = document.getElementById('btn-p3-run');
  const icon    = document.getElementById('p3-run-icon');
  const errEl   = document.getElementById('p3-error-msg');
  const product = document.getElementById('product-select')?.value || 'sereterol-activair';
  const targetCountry = 'Saudi Arabia';
  const targetRegion  = 'Middle East';

  if (btn) btn.disabled = true;
  if (icon) icon.textContent = '…';
  if (errEl) { errEl.style.display = 'none'; errEl.textContent = ''; }

  _renderP3Skeleton();
  const secEl = document.getElementById('p3-result-section');
  if (secEl) secEl.style.display = '';
  const loadEl = document.getElementById('p3-loading-state');
  if (loadEl) loadEl.style.display = '';

  try {
    const res = await fetch('/api/buyers/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ product_key: product, target_country: targetCountry, target_region: targetRegion }),
    });
    const data = await res.json();
    if (res.status !== 409 && !res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    if (_p3PollTimer) clearInterval(_p3PollTimer);
    _p3PollTimer = setInterval(_pollP3, 2500);
  } catch (e) {
    if (errEl) { errEl.style.display = ''; errEl.textContent = `오류: ${e.message}`; }
    if (btn) btn.disabled = false;
    if (icon) icon.textContent = '▶';
    if (loadEl) loadEl.style.display = 'none';
  }
}

(async function _p3AutoResume() {
  try {
    const res  = await fetch('/api/buyers/status');
    const data = await res.json();
    if (data.status === 'running') {
      const btn  = document.getElementById('btn-p3-run');
      const icon = document.getElementById('p3-run-icon');
      if (btn)  btn.disabled = true;
      if (icon) icon.textContent = '…';
      const loadEl = document.getElementById('p3-loading-state');
      if (loadEl) loadEl.style.display = '';
      if (_p3PollTimer) clearInterval(_p3PollTimer);
      _p3PollTimer = setInterval(_pollP3, 2500);
    } else if (data.status === 'done') {
      const rr     = await fetch('/api/buyers/result');
      const result = await rr.json();
      _p3Buyers  = [];
      _p3PdfName = result.pdf || null;
      const secEl = document.getElementById('p3-result-section');
      if (secEl) secEl.style.display = '';
      const cardsEl = document.getElementById('p3-cards');
      if (cardsEl) cardsEl.innerHTML = '';
    }
  } catch (_) {}
})();

async function _pollP3() {
  try {
    const res  = await fetch('/api/buyers/status');
    const data = await res.json();

    if (data.status === 'done') {
      clearInterval(_p3PollTimer); _p3PollTimer = null;
      const loadEl = document.getElementById('p3-loading-state');
      if (loadEl) loadEl.style.display = 'none';

      const rr     = await fetch('/api/buyers/result');
      const result = await rr.json();
      _p3Buyers  = result.items || result.buyers || [];   // 서버는 'items' 반환
      _p3PdfName = result.pdf   || null;

      _renderP3Cards(_p3Buyers);
      const secEl = document.getElementById('p3-result-section');
      if (secEl) secEl.style.display = '';
      if (_p3PdfName) {
        _addReportEntry({ trade_name: '바이어 발굴', inn: null, verdict: '—' }, _p3PdfName, 'p3');
      }

      // 사우디 전용: 바이어 발굴 완료 후 경쟁사 맵 + White-Space 자동 실행
      try { _autoRunSaudiPanels(null); } catch (_) {}
      const btn  = document.getElementById('btn-p3-run');
      const icon = document.getElementById('p3-run-icon');
      if (btn)  btn.disabled = false;
      if (icon) icon.textContent = '▶';

    } else if (data.status === 'error') {
      clearInterval(_p3PollTimer); _p3PollTimer = null;
      const errEl = document.getElementById('p3-error-msg');
      if (errEl) { errEl.style.display = ''; errEl.textContent = `오류: ${data.step_label || '파이프라인 실패'}`; }
      const loadEl = document.getElementById('p3-loading-state');
      if (loadEl) loadEl.style.display = 'none';
      const btn  = document.getElementById('btn-p3-run');
      const icon = document.getElementById('p3-run-icon');
      if (btn)  btn.disabled = false;
      if (icon) icon.textContent = '▶';
    }
  } catch (_) {}
}

function _renderP3Skeleton() {
  const wrap = document.getElementById('p3-cards');
  if (!wrap) return;
  wrap.innerHTML = Array.from({ length: 10 }, (_, i) => `
    <div class="p3-list-row" style="pointer-events:none;">
      <span class="p3-card-rank" style="opacity:.25;">${i + 1}</span>
      <div class="p3-skel-line" style="height:13px;width:55%;border-radius:4px;"></div>
    </div>`).join('');
}

function _renderP3Cards(buyers) {
  const wrap = document.getElementById('p3-cards');
  if (!wrap) return;

  _p3DisplayedBuyers = buyers;

  if (!buyers.length) {
    wrap.innerHTML = '<div class="p3-empty">발굴된 바이어가 없습니다.</div>';
    return;
  }

  wrap.innerHTML = buyers.map((b, i) => `
    <div class="p3-list-row" onclick="showBuyerDetail(${i})">
      <span class="p3-card-rank">${i + 1}</span>
      <span class="p3-list-name">${_escHtml(b.company_name || '-')}</span>
    </div>`).join('');

  const criteriaBox = document.getElementById('p3-criteria-box');
  const cardsTitle  = document.getElementById('p3-cards-title');
  const reportBar   = document.getElementById('p3-report-bar');
  if (criteriaBox) criteriaBox.style.display = '';
  if (cardsTitle)  cardsTitle.style.display  = '';
  if (reportBar)   reportBar.style.display   = '';
}

function showBuyerDetail(idx) {
  const b = _p3DisplayedBuyers[idx] || _p3Buyers[idx];
  if (!b) return;
  const e = b.enriched || {};
  const priLabel = b.priority === 1 ? '성분 일치' : 'Saudi Arabia';
  const priClass = b.priority === 1 ? 'p3-tag-p1' : 'p3-tag-p2';

  function row(label, val) {
    if (!val || val === '-' || val === null || val === undefined) return '';
    return `<tr><th>${label}</th><td>${_escHtml(String(val))}</td></tr>`;
  }
  function ynRow(label, val) {
    if (val === true)  return `<tr><th>${label}</th><td><span class="bm-yes">✓ 있음</span></td></tr>`;
    if (val === false) return `<tr><th>${label}</th><td><span class="bm-no">✗ 없음</span></td></tr>`;
    return '';
  }

  const hasSource = (e.source_urls || []).length > 0 || !!b.perplexity_text;
  const matched    = (b.matched_ingredients || []).join(' · ');
  const territories = (e.territories || []).join(', ');
  const metaParts = [b.country, b.category].filter(v => v && v !== '-').map(v => _escHtml(v)).join(' · ');

  const contactRows = [
    row('주소', b.address), row('전화', b.phone), row('팩스', b.fax),
    row('이메일', b.email), row('웹사이트', b.website),
  ].join('');
  const sizeRows = [
    row('연 매출', e.revenue), row('임직원 수', e.employees), row('설립연도', e.founded),
    territories ? `<tr><th>사업 지역</th><td>${_escHtml(territories)}</td></tr>` : '',
  ].join('');
  const capRows = [
    ynRow('GMP 인증', e.has_gmp), ynRow('수입 이력', e.import_history), ynRow('공공조달 이력', e.procurement_history),
  ].join('');
  const channelRows = [
    ynRow('공공 채널', e.public_channel), ynRow('민간 채널', e.private_channel),
    ynRow('약국 체인', e.has_pharmacy_chain), ynRow('MAH 대행', e.mah_capable),
    row('한국 거래 경험', e.korea_experience),
  ].join('');

  const overview = (e.company_overview_kr || '').trim();
  const reason   = (e.recommendation_reason || '').trim();

  document.getElementById('buyer-modal-body').innerHTML = `
    <div class="bm-header">
      <div class="bm-rank">${idx+1}</div>
      <div class="bm-title">
        <div class="bm-name">${_escHtml(b.company_name || '-')}</div>
        <div class="bm-meta">${metaParts}
          <span class="p3-tag ${priClass}" style="margin-left:6px;">${priLabel}</span>
        </div>
      </div>
    </div>
    ${overview && overview !== '-' ? `<div class="bm-section">기업 개요</div><div class="bm-summary">${_escHtml(overview)}</div>` : ''}
    ${reason  && reason  !== '-' ? `<div class="bm-section">채택 이유</div><div class="bm-summary">${_escHtml(reason)}</div>`  : ''}
    ${contactRows ? `<div class="bm-section">연락처</div><table class="bm-table">${contactRows}</table>` : ''}
    ${sizeRows    ? `<div class="bm-section">기업 규모</div><table class="bm-table">${sizeRows}</table>` : ''}
    ${capRows     ? `<div class="bm-section">역량 · 실적</div><table class="bm-table">${capRows}</table>` : ''}
    ${channelRows ? `<div class="bm-section">채널 · 파트너 적합성</div><table class="bm-table">${channelRows}</table>` : ''}
    ${matched   ? `<div class="bm-section">성분 매칭</div><div class="bm-match">🧪 ${_escHtml(matched)}</div>` : ''}
    ${hasSource ? `<div class="bm-section">출처</div><div class="bm-sources">AI 분석</div>` : ''}
  `;

  const overlay = document.getElementById('buyer-modal-overlay');
  if (overlay) { overlay.style.display = 'flex'; document.body.style.overflow = 'hidden'; }
}

function p3ReRank() {
  const cbs = [...document.querySelectorAll('.p3-cb:checked')];
  if (!cbs.length) { _renderP3Cards([..._p3Buyers]); return; }
  const scored = _p3Buyers.map(b => {
    const scores = b.scores || {};
    const e = b.enriched || {};
    let total = 0;
    cbs.forEach(cb => {
      const key = cb.dataset.key;
      const w   = parseFloat(cb.dataset.weight) || 0;
      const v   = key === 'pharmacy_chain' ? (e.has_pharmacy_chain ? 100 : 0) : (scores[key] || 0);
      total += (v * w) / 100;
    });
    return { ...b, _rerank: total };
  });
  scored.sort((a, b) => b._rerank - a._rerank);
  _renderP3Cards(scored);
}

function p3ClearAll() {
  document.querySelectorAll('.p3-cb').forEach(cb => cb.checked = false);
  _renderP3Cards([..._p3Buyers]);
}

function closeBuyerModal(e) {
  if (e && e.target !== document.getElementById('buyer-modal-overlay')) return;
  const overlay = document.getElementById('buyer-modal-overlay');
  if (overlay) overlay.style.display = 'none';
  document.body.style.overflow = '';
}

/** URL 해시 → 페이지 id */
const _TAB_FROM_HASH = {
  '':      'main',
  '#main': 'main',
  '#rep':  'rep',
};

function initTabFromHash() {
  const pageId = _TAB_FROM_HASH[location.hash] ?? 'main';
  goTab(pageId, document.getElementById('tab-' + pageId));
}

window.addEventListener('hashchange', initTabFromHash);

/* ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   §13. 초기화
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */

loadMacro();               // 거시지표 (§2)
loadKeyStatus();           // §6: API 키 배지
loadExchange();            // §3: 환율 즉시 로드
renderReportTab();         // §5: 보고서 탭 초기 렌더
loadNews();                // §11: 뉴스 즉시 로드
populateP2ReportSelect();
initP2Dropzone();
_bindP2Inputs();
_p2UpdateManualNote();
_p2UpdateRunEnabled();
_p3RenderCache();
initTabFromHash();
_syncP3ReportOptions();
