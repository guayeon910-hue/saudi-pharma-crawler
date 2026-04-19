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

let _pollTimer  = null;   // 파이프라인 폴링 타이머
let _currentKey = null;   // 현재 선택된 product_key

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
const TODO_LS_KEY    = 'sg_upharma_todos_v1';
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

const REPORTS_LS_KEY = 'sg_upharma_reports_v1';
const REPORTS_FULL_LS_KEY = 'sg_upharma_reports_full_v1';

let _p2LastRequestState = null;
let _p2OverrideTimer = null;

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
function _addReportEntry(result, pdfName) {
  const reports = _loadReports();
  const entryId = Date.now();
  const entry   = {
    id:        entryId,
    product:   result ? (result.trade_name || result.product_id || '알 수 없음') : '알 수 없음',
    inn:       result ? (INN_MAP[result.product_id] || result.inn || '') : '',
    verdict:   result ? (result.verdict || '—') : '—',
    timestamp: new Date().toLocaleString('ko-KR', {
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

  // UI 초기화
  resetProgress();
  document.getElementById('result-card').classList.remove('visible');
  document.getElementById('papers-card').classList.remove('visible');
  document.getElementById('report-card').classList.remove('visible');
  document.getElementById('btn-analyze').disabled = true;
  document.getElementById('btn-icon').textContent  = '⏳';

  const reBtn = document.getElementById('btn-reanalyze');
  if (reBtn) reBtn.style.display = 'none';

  // B2: db_load 단계 먼저 활성화
  setProgress('db_load', 'running');

  try {
    const res = await fetch(`/api/pipeline/${encodeURIComponent(productKey)}`, { method: 'POST' });
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      console.error('파이프라인 오류:', d.detail || res.status);
      setProgress('db_load', 'error');
      _resetBtn();
      return;
    }
    _pollTimer = setInterval(() => pollPipeline(productKey), 2500);
  } catch (e) {
    console.error('요청 실패:', e);
    setProgress('db_load', 'error');
    _resetBtn();
  }
}

function _resetBtn() {
  document.getElementById('btn-analyze').disabled = false;
  document.getElementById('btn-icon').textContent  = '▶';
}

/**
 * GET /api/pipeline/{product_key}/status 를 주기적으로 폴링.
 * 서버 step: init → db_load → analyze → refs → report → done
 */
async function pollPipeline(productKey) {
  try {
    const res = await fetch(`/api/pipeline/${encodeURIComponent(productKey)}/status`);
    const d   = await res.json();

    if (d.status === 'idle') return;

    // B2: 서버 step → 프론트 STEP_ORDER 매핑
    if      (d.step === 'db_load')  { setProgress('db_load',  'running'); }
    else if (d.step === 'analyze')  { setProgress('db_load',  'done'); setProgress('analyze', 'running'); }
    else if (d.step === 'refs')     { setProgress('analyze',  'done'); setProgress('refs',    'running'); }
    else if (d.step === 'report')   {
      setProgress('refs', 'done'); setProgress('report', 'running');
      _showReportLoading();
    }

    if (d.status === 'done') {
      clearInterval(_pollTimer);
      for (const s of STEP_ORDER) setProgress(s, 'done');
      const r2   = await fetch(`/api/pipeline/${encodeURIComponent(productKey)}/result`);
      const data = await r2.json();
      renderResult(data.result, data.refs, data.pdf);
      _resetBtn();
    }

    if (d.status === 'error') {
      clearInterval(_pollTimer);
      setProgress(STEP_ORDER.includes(d.step) ? d.step : 'analyze', 'error');
      _resetBtn();
    }
  } catch (_) { /* 조용히 재시도 */ }
}

/* ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   §9. 신약 분석 파이프라인
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */

let _customPollTimer = null;
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
}

async function runCustomPipeline() {
  const tradeName = document.getElementById('custom-trade-name').value.trim();
  const inn       = document.getElementById('custom-inn').value.trim();
  const dosage    = document.getElementById('custom-dosage').value.trim();
  if (!tradeName || !inn) { alert('약품명과 성분명을 입력하세요.'); return; }

  _resetCustomProgress();
  document.getElementById('result-card').classList.remove('visible');
  document.getElementById('papers-card').classList.remove('visible');
  document.getElementById('report-card').classList.remove('visible');
  document.getElementById('btn-custom').disabled = true;
  document.getElementById('custom-icon').textContent = '⏳';

  if (_customPollTimer) clearInterval(_customPollTimer);
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
    _customPollTimer = setInterval(_pollCustomPipeline, 2500);
  } catch (e) {
    console.error('요청 실패:', e);
    _setCustomProgress('analyze', 'error');
    _resetCustomBtn();
  }
}

async function _pollCustomPipeline() {
  try {
    const res = await fetch('/api/pipeline/custom/status');
    const d   = await res.json();
    if (d.status === 'idle') return;

    if      (d.step === 'analyze') { _setCustomProgress('analyze', 'running'); }
    else if (d.step === 'refs')    { _setCustomProgress('analyze', 'done'); _setCustomProgress('refs', 'running'); }
    else if (d.step === 'report')  { _setCustomProgress('refs', 'done'); _setCustomProgress('report', 'running'); _showReportLoading(); }

    if (d.status === 'done') {
      clearInterval(_customPollTimer);
      for (const s of CUSTOM_STEP_ORDER) _setCustomProgress(s, 'done');
      const r2   = await fetch('/api/pipeline/custom/result');
      const data = await r2.json();
      renderResult(data.result, data.refs, data.pdf);
      _resetCustomBtn();
    }
    if (d.status === 'error') {
      clearInterval(_customPollTimer);
      _setCustomProgress(d.step || 'analyze', 'error');
      _resetCustomBtn();
    }
  } catch (_) { /* 조용히 재시도 */ }
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

    // N3: 1공정 완료 → Todo 자동 체크
    markTodoDone('p1');
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
    _addReportEntry(result, pdfName);
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
  const stub = document.getElementById('p2-result-stub');
  const host = document.getElementById('p2-result-area');
  if (stub) {
    stub.style.display = 'none';
    if (clear) stub.textContent = '';
  }
  if (host) {
    host.style.display = 'none';
    if (clear) host.innerHTML = '';
  }
  if (clear) {
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
    : '직접 입력은 현재 공공 시장 스텁 경로에서만 참고용으로 지원됩니다.';
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

  const hPub = document.getElementById('p2-market-hint-public');
  const hPrv = document.getElementById('p2-market-hint-private');
  if (hPub) hPub.style.display = _p2Market === 'public' ? 'block' : 'none';
  if (hPrv) hPrv.style.display = _p2Market === 'private' ? 'block' : 'none';

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
    const label = `${_escHtml(r.product)} · ${_escHtml(r.timestamp)}`;
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

function _p2RenderScenarioCard(name, scenario) {
  const fobClass = scenario.error ? ' is-error' : '';
  return `
    <article class="p2-scenario-card${fobClass}">
      <div class="p2-scenario-head">
        <div>
          <div class="p2-scenario-label">${_escHtml(scenario.label || name)}</div>
          <div class="p2-scenario-meta">
            ${_escHtml(`진입 순위 ${scenario.entry_rank} · ${String(scenario.retail_basis || '').toUpperCase()} 기준 · Tier ${scenario.tier}`)}
          </div>
        </div>
        <span class="p2-rank-badge">${_escHtml(`${scenario.entry_rank}위`)}</span>
      </div>

      <div class="p2-scenario-fob">${_p2FormatMoney(scenario.fob_sar, 'SAR')}</div>
      <div class="p2-scenario-currencies">
        <span>${_p2FormatMoney(scenario.fob_usd, 'USD')}</span>
        <span>${_p2FormatMoney(scenario.fob_krw, 'KRW')}</span>
      </div>

      <div class="p2-scenario-stats">
        <span>Retail ${_p2FormatMoney(scenario.retail_sar, 'SAR')}</span>
        <span>운임 ${_p2FormatMoney(scenario.freight_insurance_sar, 'SAR')}</span>
        <span>커미션 ${_p2FormatPct(scenario.agent_commission_pct)}</span>
        <span>Port fee ${_p2FormatMoney(scenario.port_fee_sar, 'SAR')}</span>
      </div>

      ${scenario.error ? `<div class="p2-scenario-error">${_escHtml(scenario.error)}</div>` : ''}

      <details class="p2-steps-details">
        <summary>단계별 역산 보기</summary>
        ${_p2RenderStepsTable(scenario.steps)}
      </details>

      <div class="p2-overrides" data-scenario-card="${_escHtml(name)}">
        <div class="p2-overrides-title">시나리오 오버라이드</div>
        <div class="p2-override-field">
          <div class="p2-override-label">
            에이전트 커미션
            <span class="p2-override-value"></span>
          </div>
          <input
            type="range"
            min="0"
            max="15"
            step="0.5"
            value="${Number((scenario.agent_commission_pct || 0) * 100).toFixed(1)}"
            class="p2-override-input"
            data-scenario="${_escHtml(name)}"
            data-field="agent_commission_pct"
          />
        </div>
        <div class="p2-override-field">
          <div class="p2-override-label">
            운임 배수
            <span class="p2-override-value"></span>
          </div>
          <input
            type="range"
            min="0.5"
            max="2"
            step="0.05"
            value="${Number(scenario.freight_multiplier || 1).toFixed(2)}"
            class="p2-override-input"
            data-scenario="${_escHtml(name)}"
            data-field="freight_multiplier"
          />
        </div>
      </div>
    </article>`;
}

function _p2SyncOverrideDisplay(input) {
  const valueEl = input.closest('.p2-override-field')?.querySelector('.p2-override-value');
  if (!valueEl) return;
  if (input.dataset.field === 'agent_commission_pct') {
    valueEl.textContent = `${Number(input.value).toFixed(1)}%`;
  } else {
    valueEl.textContent = `${Number(input.value).toFixed(2)}x`;
  }
}

function _p2CollectOverridesFromDom() {
  const scenarios = {};
  document.querySelectorAll('.p2-override-input').forEach(input => {
    const scenario = input.dataset.scenario;
    const field = input.dataset.field;
    if (!scenario || !field) return;
    if (!scenarios[scenario]) scenarios[scenario] = {};
    const raw = Number(input.value);
    scenarios[scenario][field] = field === 'agent_commission_pct' ? raw / 100 : raw;
  });
  return { scenarios };
}

function _p2BindOverrideInputs() {
  document.querySelectorAll('.p2-override-input').forEach(input => {
    _p2SyncOverrideDisplay(input);
    input.addEventListener('input', () => {
      _p2SyncOverrideDisplay(input);
      if (_p2OverrideTimer) clearTimeout(_p2OverrideTimer);
      _p2OverrideTimer = setTimeout(() => {
        if (!_p2LastRequestState || _p2LastRequestState.marketType !== 'private') return;
        _submitP2Analysis({ useLastState: true, overrides: _p2CollectOverridesFromDom() });
      }, 300);
    });
  });
}

function renderP2Result(data) {
  const host = document.getElementById('p2-result-area');
  if (!host) return;

  const stats = data.competitor_stats || {};
  const product = data.product || {};
  const classification = data.classification || {};
  const scenarios = data.scenarios || {};
  const notes = data.notes || [];
  const reg = data.regulatory_cost || {};
  const scenarioOrder = ['aggressive', 'average', 'conservative'];

  host.innerHTML = `
    <section class="p2-result-summary">
      <div class="p2-summary-main">
        <div class="p2-summary-title">${_escHtml(product.trade_name || '민간 시장 FOB 분석')}</div>
        <div class="p2-summary-sub">${_escHtml([product.inn, product.strength, product.dosage_form].filter(Boolean).join(' · '))}</div>
        <div class="p2-summary-tags">
          <span class="p2-summary-tag">${_escHtml(`분류 ${classification.product_kind || 'generic'}`)}</span>
          <span class="p2-summary-tag">${classification.is_combination ? '복합제' : '단일 성분'}</span>
          <span class="p2-summary-tag">${classification.is_extended_release ? '서방형/개량신약' : '표준 제형'}</span>
          <span class="p2-summary-tag">${_escHtml(product.hs_code || 'HS 코드 미상')}</span>
        </div>
        <p class="p2-summary-rationale">${_escHtml(classification.rationale || '')}</p>
        ${_p2RenderWarnings(classification.warnings)}
      </div>
      <div class="p2-summary-side">
        <div class="p2-stat-box">
          <div class="p2-stat-label">경쟁가 분포</div>
          <div class="p2-stat-line">min ${_p2FormatMoney(stats.min, 'SAR')}</div>
          <div class="p2-stat-line">p25 ${_p2FormatMoney(stats.p25, 'SAR')}</div>
          <div class="p2-stat-line">median ${_p2FormatMoney(stats.median, 'SAR')}</div>
          <div class="p2-stat-line">p75 ${_p2FormatMoney(stats.p75, 'SAR')}</div>
          <div class="p2-stat-line">max ${_p2FormatMoney(stats.max, 'SAR')}</div>
          <div class="p2-stat-line">avg ${_p2FormatMoney(stats.avg, 'SAR')}</div>
          ${stats.warning ? `<div class="p2-stat-warning">${_escHtml(stats.warning)}</div>` : ''}
        </div>
      </div>
    </section>

    <section class="p2-scenarios-grid">
      ${scenarioOrder.map(name => _p2RenderScenarioCard(name, scenarios[name] || {})).join('')}
    </section>

    <section class="p2-reg-cost">
      <div class="p2-reg-head">
        <div class="p2-reg-title">규제비 감가상각 요약</div>
        <div class="p2-reg-value">${_p2FormatMoney(reg.per_unit_amortization_sar, 'SAR')} / unit</div>
      </div>
      <div class="p2-reg-grid">
        <div>SFDA 등록비 ${_p2FormatMoney(reg.sfda_registration_sar, 'SAR')}</div>
        <div>SABER PCoC ${_p2FormatMoney(reg.saber_pcoc_annual_sar, 'SAR')}</div>
        <div>SABER SCoC(연간) ${_p2FormatMoney(reg.saber_scoc_annual_sar, 'SAR')}</div>
        <div>가정 연간 수량 ${Number(reg.assumptions?.annual_units || 0).toLocaleString('ko-KR')}</div>
        <div>가정 월 선적 ${Number(reg.assumptions?.monthly_shipments || 0).toLocaleString('ko-KR')}</div>
      </div>
      <div class="p2-notes">
        ${notes.map(note => `<div class="p2-note-item">${_escHtml(note)}</div>`).join('')}
      </div>
    </section>`;

  host.style.display = 'block';
  _p2BindOverrideInputs();
}

async function _submitP2Analysis({ useLastState = false, overrides = null } = {}) {
  const btn  = document.getElementById('btn-p2-ai-run');
  const icon = document.getElementById('p2-ai-run-icon');
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

  _p2LastPayload = {
    input_mode: _p2InputMode,
    market_type: _p2Market,
    report_id: (document.getElementById('p2-report-select') || {}).value || null,
    report_data: reportFullBlob,
    manual_product: _p2InputMode === 'manual' ? document.getElementById('p2-manual-product').value.trim() : null,
    pdf_file: (document.getElementById('p2-pdf-input')?.files || [])[0] || null,
  };

  await _p2PerformRequest(fd);
}

async function _p2PerformRequest(fd) {
  if (_p2Running) return;
  _p2Running = true;
  const btn  = document.getElementById('p2-btn-run');
  const icon = document.getElementById('p2-btn-icon');
  if (btn) btn.disabled = true;
  if (icon) icon.textContent = '⏳';

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

    const stub = document.getElementById('p2-result-stub');
    if (data.ok && stub) {
      stub.textContent = data.message || '처리되었습니다.';
      stub.style.display = 'block';
      return;
    }

    _p2ShowError(data.detail || data.message || '응답을 해석할 수 없습니다.');
  } catch (e) {
    console.warn('2공정 분석 요청 실패:', e);
    _p2ShowError('네트워크 오류 — 잠시 후 다시 시도해 주세요.');
  } finally {
    _p2Running = false;
    if (icon) icon.textContent = '▶';
    _p2UpdateRunEnabled();
  }
}

async function runP2PriceAnalysis() {
  return _submitP2Analysis();
}

/* ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   §12b. 3공정 · 바이어 발굴 (Perplexity prospects)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */

const P3_LS_KEY = 'sg_upharma_p3_prospects_v1';

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
