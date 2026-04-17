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
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */

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
  if (id === 'p2') populateP2ReportSelect();
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

const REPORTS_LS_KEY      = 'sg_upharma_reports_v1';
const REPORTS_FULL_LS_KEY = 'sg_upharma_reports_full_v1';   // 2공정 민간 FOB 재분석용 최소 blob

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
  const id      = Date.now();
  const entry   = {
    id,
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

  // full blob은 요약 리스트의 id 집합만 유지 (잘린 항목 자동 정리)
  const full = _loadReportsFull();
  const blob = _buildReportFullBlob(result);
  if (blob) full[String(id)] = blob;
  const keepIds = new Set(trimmed.map(r => String(r.id)));
  for (const k of Object.keys(full)) if (!keepIds.has(k)) delete full[k];
  _saveReportsFull(full);

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
  _p2HideResults();
  _p2UpdateRunEnabled();
}

function setP2Market(market, el) {
  _p2Market = market === 'private' ? 'private' : 'public';
  const pub = document.getElementById('p2-market-public');
  const prv = document.getElementById('p2-market-private');
  if (pub) pub.classList.toggle('on', _p2Market === 'public');
  if (prv) prv.classList.toggle('on', _p2Market === 'private');

  const hPub = document.getElementById('p2-market-hint-public');
  const hPrv = document.getElementById('p2-market-hint-private');
  if (hPub) hPub.style.display = _p2Market === 'public' ? 'block' : 'none';
  if (hPrv) hPrv.style.display = _p2Market === 'private' ? 'block' : 'none';

  _p2HideError();
  _p2HideResults();
  _p2UpdateRunEnabled();
}

function populateP2ReportSelect() {
  const sel = document.getElementById('p2-report-select');
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
  const el = document.getElementById('p2-inline-error');
  if (el) { el.style.display = 'none'; el.textContent = ''; }
}

function _p2ShowError(msg) {
  const el = document.getElementById('p2-inline-error');
  if (!el) return;
  el.textContent = msg;
  el.style.display = 'block';
}

function _p2UpdateRunEnabled() {
  const btn = document.getElementById('p2-btn-run');
  if (!btn) return;
  let ok = false;
  if (_p2InputMode === 'ai') {
    const sel = document.getElementById('p2-report-select');
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
  const sel = document.getElementById('p2-report-select');
  if (sel) sel.addEventListener('change', () => { _p2HideError(); _p2UpdateRunEnabled(); });
  const man = document.getElementById('p2-manual-product');
  if (man) man.addEventListener('input', () => { _p2HideError(); _p2UpdateRunEnabled(); });
}

let _p2LastPayload = null;   // override 재호출용 (파일 원본 포함)
let _p2OverrideTimer = null; // debounce
let _p2Running = false;

function _p2HideResults() {
  const pub = document.getElementById('p2-result-public');
  const prv = document.getElementById('p2-result-private');
  if (pub) pub.style.display = 'none';
  if (prv) prv.style.display = 'none';
  const stub = document.getElementById('p2-result-stub');
  if (stub) stub.style.display = 'none';
}

async function runP2PriceAnalysis() {
  const btn  = document.getElementById('p2-btn-run');
  const icon = document.getElementById('p2-btn-icon');
  _p2HideError();
  _p2HideResults();

  if (_p2InputMode === 'manual' && _p2Market === 'private') {
    _p2ShowError('민간 시장 분석은 저장된 1공정 보고서 또는 PDF가 필요합니다. 직접 입력 모드는 v1에서 공공 시장만 지원합니다.');
    return;
  }

  let reportFullBlob = null;
  if (_p2InputMode === 'ai') {
    const sel = document.getElementById('p2-report-select');
    const inp = document.getElementById('p2-pdf-input');
    const hasRep = sel && sel.value;
    const hasPdf = inp && inp.files && inp.files[0];
    if (!hasRep && !hasPdf) {
      _p2ShowError('저장된 1공정 보고서를 선택하거나 PDF를 업로드하세요.');
      return;
    }
    if (hasRep) {
      const full = _loadReportsFull();
      reportFullBlob = full[String(sel.value)] || null;
      if (!reportFullBlob && _p2Market === 'private') {
        _p2ShowError('이 보고서는 2공정 재분석에 필요한 데이터가 없습니다 (구버전). 1공정을 다시 실행하거나 PDF 업로드를 사용하세요.');
        return;
      }
    }
  } else {
    const man = document.getElementById('p2-manual-product');
    if (!man || !man.value.trim()) {
      _p2ShowError('품목명을 입력하세요.');
      return;
    }
  }

  const fd = new FormData();
  fd.append('input_mode', _p2InputMode);
  fd.append('market_type', _p2Market);
  if (_p2InputMode === 'ai') {
    const sel = document.getElementById('p2-report-select');
    if (sel && sel.value) fd.append('report_id', sel.value);
    if (reportFullBlob) fd.append('report_data', JSON.stringify(reportFullBlob));
    const inp = document.getElementById('p2-pdf-input');
    if (inp && inp.files && inp.files[0]) fd.append('pdf', inp.files[0]);
  } else {
    fd.append('manual_product', document.getElementById('p2-manual-product').value.trim());
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
    const res = await fetch('/api/p2/price-analyze', { method: 'POST', body: fd });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.ok === false) {
      _p2ShowError(data.detail || data.message || `요청 실패 (${res.status})`);
      return;
    }
    if ((data.market_type || _p2Market) === 'private') {
      _p2RenderPrivate(data);
    } else {
      _p2RenderPublicStub(data);
    }
  } catch (e) {
    console.warn('2공정 분석 요청 실패:', e);
    _p2ShowError('네트워크 오류 — 잠시 후 다시 시도해 주세요.');
  } finally {
    _p2Running = false;
    if (icon) icon.textContent = '▶';
    _p2UpdateRunEnabled();
  }
}

function _p2RebuildFormData(overrides) {
  const p = _p2LastPayload;
  if (!p) return null;
  const fd = new FormData();
  fd.append('input_mode', p.input_mode);
  fd.append('market_type', p.market_type);
  if (p.report_id) fd.append('report_id', p.report_id);
  if (p.report_data) fd.append('report_data', JSON.stringify(p.report_data));
  if (p.manual_product) fd.append('manual_product', p.manual_product);
  if (p.pdf_file) fd.append('pdf', p.pdf_file);
  if (overrides && Object.keys(overrides).length) {
    fd.append('overrides', JSON.stringify(overrides));
  }
  return fd;
}

function _p2ScheduleOverrideRerun() {
  if (_p2OverrideTimer) clearTimeout(_p2OverrideTimer);
  _p2OverrideTimer = setTimeout(() => {
    const overrides = _p2CollectOverrides();
    const fd = _p2RebuildFormData(overrides);
    if (fd) _p2PerformRequest(fd);
  }, 300);
}

function _p2CollectOverrides() {
  const out = {};
  for (const scen of ['aggressive', 'average', 'conservative']) {
    const a = document.getElementById(`p2-ov-agent-${scen}`);
    const f = document.getElementById(`p2-ov-freight-${scen}`);
    const entry = {};
    if (a && a.value.trim() !== '') {
      const v = parseFloat(a.value);
      if (!Number.isNaN(v)) entry.agent_commission_pct = v / 100;
    }
    if (f && f.value.trim() !== '') {
      const v = parseFloat(f.value);
      if (!Number.isNaN(v)) entry.freight_multiplier = v;
    }
    if (Object.keys(entry).length) out[scen] = entry;
  }
  return out;
}

function _p2RenderPublicStub(data) {
  const stub = document.getElementById('p2-result-stub');
  if (!stub) return;
  stub.textContent = data.message || '공공 시장 파이프라인은 곧 연결됩니다.';
  stub.style.display = 'block';
}

const _P2_SCEN_META = {
  aggressive:   { label: '공격적 (1위)',    cls: 'p2-scen-agg'  },
  average:      { label: '평균 (2위)',      cls: 'p2-scen-avg'  },
  conservative: { label: '보수적 (3위)',    cls: 'p2-scen-con'  },
};

function _fmtSar(v) { return v == null ? '—' : `${Number(v).toFixed(2)} SAR`; }
function _fmtUsd(v) { return v == null ? '—' : `${Number(v).toFixed(2)} USD`; }
function _fmtKrw(v) { return v == null ? '—' : `${Math.round(Number(v)).toLocaleString('ko-KR')} KRW`; }
function _fmtPct(v) { return `${(Number(v || 0) * 100).toFixed(1)}%`; }

function _p2RenderPrivate(data) {
  const host = document.getElementById('p2-result-private');
  if (!host) return;
  host.style.display = 'block';
  host.innerHTML = '';

  const prod = data.product || {};
  const cls  = data.classification || {};
  const stats = data.competitor_stats || {};
  const fx    = data.exchange_rates || {};
  const reg   = data.regulatory_cost || {};
  const notes = Array.isArray(data.notes) ? data.notes : [];
  const scens = Array.isArray(data.scenarios) ? data.scenarios : [];

  const header = document.createElement('div');
  header.className = 'p2-priv-header';
  header.innerHTML = `
    <div class="p2-priv-title">
      <div class="p2-priv-name">${_escHtml(prod.trade_name || '알 수 없음')}</div>
      <div class="p2-priv-meta">
        ${_escHtml(prod.ingredient || '')} · ${_escHtml(prod.strength || '')} · ${_escHtml(prod.dosage_form || '')}
      </div>
    </div>
    <div class="p2-priv-tags">
      <span class="p2-tag">${_escHtml(cls.product_kind || 'generic')}</span>
      ${cls.is_combination ? '<span class="p2-tag p2-tag-warn">복합제</span>' : ''}
      ${cls.is_extended_release ? '<span class="p2-tag">ER/CR</span>' : ''}
      <span class="p2-tag p2-tag-muted">HS ${_escHtml(prod.hs_code || '미확정')}</span>
    </div>
  `;
  host.appendChild(header);

  // 시나리오 카드 3개
  const row = document.createElement('div');
  row.className = 'p2-scen-row';
  scens.forEach(s => {
    const meta = _P2_SCEN_META[s.scenario] || { label: s.label || s.scenario, cls: '' };
    const fob  = s.fob || {};
    const steps = (s.steps || []).map(st => `
      <tr>
        <td>${_escHtml(st.label || st.step)}</td>
        <td class="num">${_fmtSar(st.sar)}</td>
        <td class="num">${_fmtUsd(st.usd)}</td>
        <td class="num">${_fmtKrw(st.krw)}</td>
      </tr>
    `).join('');
    const errBlock = s.error
      ? `<div class="p2-scen-error">${_escHtml(s.error)}</div>`
      : '';
    const card = document.createElement('article');
    card.className = `p2-scen-card ${meta.cls}`;
    card.innerHTML = `
      <div class="p2-scen-head">
        <div class="p2-scen-label">${_escHtml(meta.label)}</div>
        <div class="p2-scen-rank">rank ${s.rank}</div>
      </div>
      <div class="p2-scen-retail">Retail base: ${_fmtSar(s.retail_sar)} <span class="p2-muted">(${_escHtml(s.retail_base_key)})</span></div>
      <div class="p2-scen-fob">
        <div class="p2-scen-fob-main">${_fmtSar(fob.sar)}</div>
        <div class="p2-scen-fob-sub">${_fmtUsd(fob.usd)} · ${_fmtKrw(fob.krw)}</div>
      </div>
      ${errBlock}
      <details class="p2-scen-steps">
        <summary>단계별 역산 보기</summary>
        <table class="p2-scen-table">
          <thead><tr><th>단계</th><th>SAR</th><th>USD</th><th>KRW</th></tr></thead>
          <tbody>${steps}</tbody>
        </table>
      </details>
      <div class="p2-scen-overrides">
        <label>에이전트 수수료(%)
          <input type="number" step="0.1" min="0" max="50"
                 id="p2-ov-agent-${s.scenario}"
                 value="${(Number(s.agent_commission_pct || 0) * 100).toFixed(1)}"/>
        </label>
        <label>운임 배수
          <input type="number" step="0.05" min="0" max="5"
                 id="p2-ov-freight-${s.scenario}"
                 value="${Number(s.freight_multiplier || 1).toFixed(2)}"/>
        </label>
      </div>
      <div class="p2-scen-foot">
        port fee: ${_fmtSar(s.port_fee_sar)} · freight base: ${_fmtSar(s.freight_base_sar)}
      </div>
    `;
    row.appendChild(card);
  });
  host.appendChild(row);

  // 경쟁가 분포
  const dist = document.createElement('article');
  dist.className = 'p2-priv-panel';
  const samplesHtml = (stats.samples || []).map(sm => `
    <tr>
      <td>${_escHtml(sm.trade_name || '—')}</td>
      <td>${_escHtml(sm.strength || '')}</td>
      <td class="num">${_fmtSar(sm.price_sar)}</td>
      <td>${_escHtml(sm.origin || '')}</td>
    </tr>
  `).join('');
  dist.innerHTML = `
    <div class="p2-priv-panel-head">경쟁가 분포 (${stats.count || 0}건 · ${_escHtml(stats.mode || '')})</div>
    <div class="p2-priv-quartiles">
      <div><span>p25</span>${_fmtSar(stats.p25_sar)}</div>
      <div><span>median</span>${_fmtSar(stats.median_sar)}</div>
      <div><span>p75</span>${_fmtSar(stats.p75_sar)}</div>
    </div>
    <table class="p2-scen-table">
      <thead><tr><th>제품</th><th>함량</th><th>가격</th><th>출처</th></tr></thead>
      <tbody>${samplesHtml || '<tr><td colspan="4" class="p2-muted">표본 없음</td></tr>'}</tbody>
    </table>
  `;
  host.appendChild(dist);

  // 규제비/환율 패널
  const reg2 = document.createElement('article');
  reg2.className = 'p2-priv-panel';
  reg2.innerHTML = `
    <div class="p2-priv-panel-head">규제비 · 환율</div>
    <div class="p2-priv-reg">
      <div>SFDA 등록 감가상각(연): ${_fmtKrw(reg.sfda_amort_per_year_krw)} / ${_fmtSar(reg.sfda_amort_per_year_sar)}</div>
      <div>SABER 등록 감가상각(연): ${_fmtKrw(reg.saber_amort_per_year_krw)} / ${_fmtSar(reg.saber_amort_per_year_sar)}</div>
      <div>항만료(시나리오별 고정): ${_fmtSar(reg.port_fee_sar)}</div>
      <div class="p2-muted">※ FOB에서 차감하지 않고 수익 모델에 별도 반영</div>
    </div>
    <div class="p2-priv-fx">
      환율: 1 SAR = ${Number(fx.sar_krw || 0).toFixed(2)} KRW · ${Number(fx.sar_usd || 0).toFixed(4)} USD
      <span class="p2-muted">(${_escHtml(fx.source || 'unknown')})</span>
    </div>
  `;
  host.appendChild(reg2);

  // 노트
  if (notes.length) {
    const n = document.createElement('article');
    n.className = 'p2-priv-panel p2-priv-notes';
    n.innerHTML = `
      <div class="p2-priv-panel-head">주의/경고</div>
      <ul>${notes.map(x => `<li>${_escHtml(x)}</li>`).join('')}</ul>
    `;
    host.appendChild(n);
  }

  // 오버라이드 입력 바인딩 (debounce 재호출)
  for (const scen of ['aggressive', 'average', 'conservative']) {
    for (const kind of ['agent', 'freight']) {
      const el = document.getElementById(`p2-ov-${kind}-${scen}`);
      if (el) el.addEventListener('input', _p2ScheduleOverrideRerun);
    }
  }
}

/** URL 해시 → 페이지 id (`/#p1`, `/#rep` 등) */
const _TAB_FROM_HASH = {
  '':       'main',
  '#main':  'main',
  '#p1':    'p1',
  '#p2':    'p2',
  '#p3':    'p3',
  '#rep':   'rep',
};

function initTabFromHash() {
  const pageId = _TAB_FROM_HASH[location.hash] ?? 'main';
  goTab(pageId, document.getElementById('tab-' + pageId));
}

window.addEventListener('hashchange', initTabFromHash);

/* ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   §13. 초기화
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */

loadKeyStatus();   // §6: API 키 배지
loadExchange();    // §3: 환율 즉시 로드
initTodo();        // §4: Todo 상태 복원
renderReportTab(); // §5: 보고서 탭 초기 렌더
populateP2ReportSelect();
initP2Dropzone();
_bindP2Inputs();
_p2UpdateRunEnabled();
initTabFromHash();
loadNews();        // §11: 시장 뉴스 즉시 로드
