(function () {
  const sampleBar = document.getElementById('sample-bar');
  const sampleDesc = document.getElementById('sample-desc');
  const policyEditor = document.getElementById('policy-editor');
  const planEditor = document.getElementById('plan-editor');
  const policyText = document.getElementById('policy-text');
  const policyTextWrap = document.getElementById('policy-text-wrap');
  const modeJsonBtn = document.getElementById('mode-json');
  const modeTextBtn = document.getElementById('mode-text');
  const runBtn = document.getElementById('run-btn');
  const inputError = document.getElementById('input-error');
  const emptyState = document.getElementById('empty-state');
  const loadingState = document.getElementById('loading-state');
  const results = document.getElementById('results');

  const roleSelect = document.getElementById('role-select');
  const roleBanner = document.getElementById('role-banner');

  let samples = [];
  let policyMode = 'json';
  let lastResult = null;
  let geminiConfigured = false;

  // Who is looking, and what that person actually needs on screen. The review
  // itself is identical for all three - only the presentation differs, so a
  // role can never change a decision.
  const ROLES = {
    operator: {
      label: 'Infrastructure operator',
      blurb: 'You are about to merge this change. Shown: the verdict, what it ' +
             'costs, and the comment to paste on the PR.',
      cards: ['impact-card', 'gate-card', 'compare-card', 'review-card', 'resources-card'],
      canRun: true,
    },
    administrator: {
      label: 'Policy administrator',
      blurb: 'You own the policy. Shown: what the policy allowed and blocked, ' +
             'and the record of every review run against it.',
      cards: ['gate-card', 'compare-card', 'impact-card', 'history-card'],
      canRun: true,
    },
    auditor: {
      label: 'Auditor · read-only',
      blurb: 'You are checking how the answer was produced. Shown: which stages ' +
             'were deterministic, where a model was involved, the data provenance, ' +
             'and the append-only record. Running a review is disabled in this view.',
      cards: ['gate-card', 'trace-card', 'method-card', 'history-card'],
      canRun: false,
    },
  };
  const ALL_CARDS = ['impact-card', 'gate-card', 'compare-card', 'review-card',
                     'trace-card', 'method-card', 'history-card', 'resources-card'];

  const HISTORY_KEY = 'pcr.review-history.v1';

  function readHistory() {
    try {
      const raw = localStorage.getItem(HISTORY_KEY);
      return raw ? JSON.parse(raw) : [];
    } catch (e) {
      return [];  // private window, blocked storage: the app still works
    }
  }

  function appendHistory(entry) {
    try {
      const all = readHistory();
      all.unshift(entry);
      localStorage.setItem(HISTORY_KEY, JSON.stringify(all.slice(0, 50)));
    } catch (e) {
      /* storage unavailable; the review itself is unaffected */
    }
  }

  function setPolicyMode(mode) {
    policyMode = mode;
    const isJson = mode === 'json';
    policyEditor.hidden = !isJson;
    policyTextWrap.hidden = isJson;
    modeJsonBtn.classList.toggle('active', isJson);
    modeTextBtn.classList.toggle('active', !isJson);
    modeJsonBtn.setAttribute('aria-pressed', String(isJson));
    modeTextBtn.setAttribute('aria-pressed', String(!isJson));
    inputError.hidden = true;
  }

  function fmtKg(v) {
    return Math.abs(v) >= 1000
      ? (Math.round(v / 10) / 100).toLocaleString(undefined, { maximumFractionDigits: 2 }) + ' tCO2eq'
      : Math.round(v).toLocaleString() + ' kgCO2eq';
  }

  function showState(which) {
    emptyState.hidden = which !== 'empty';
    loadingState.hidden = which !== 'loading';
    results.hidden = which !== 'results';
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  }

  async function loadMeta() {
    try {
      const [health, data] = await Promise.all([
        fetch('/api/healthz').then(r => r.json()),
        fetch('/api/carbon-data').then(r => r.json()),
      ]);
      geminiConfigured = !!health.gemini_configured;
      const meta = data.metadata;
      document.getElementById('engine-meta').innerHTML =
        `Carbon data: Google official ${meta.data_year} annual (retrieved ${meta.retrieved_at})<br>` +
        (health.gemini_configured
          ? 'Review writer: Gemini (live)'
          : 'Review writer: offline template (no GEMINI_API_KEY)');
      document.getElementById('footer-source').textContent =
        `${meta.source} - ${meta.source_url}`;
    } catch (e) {
      document.getElementById('engine-meta').textContent = 'metadata unavailable';
    }
  }

  async function loadSamples() {
    const res = await fetch('/api/samples');
    const body = await res.json();
    samples = body.samples;
    samples.forEach((s, i) => {
      const btn = document.createElement('button');
      btn.className = 'sample-btn';
      btn.textContent = s.title;
      btn.addEventListener('click', () => selectSample(i, btn));
      sampleBar.appendChild(btn);
    });
    if (samples.length) selectSample(0, sampleBar.firstChild);
  }

  function selectSample(i, btn) {
    sampleBar.querySelectorAll('.sample-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    const s = samples[i];
    sampleDesc.textContent = s.description;
    policyEditor.value = JSON.stringify(s.policy, null, 2);
    planEditor.value = JSON.stringify(s.plan, null, 2);
    policyText.value = s.policy_text || '';
    setPolicyMode(s.id === '4-natural-language-policy' && geminiConfigured ? 'text' : 'json');
    inputError.hidden = true;
    // Clear the previous scene's output: leaving it on screen next to a
    // different input reads as if it belongs to the new one.
    emptyState.querySelector('p').textContent =
      'Pick a sample scenario on the left and run the review.';
    lastResult = null;
    applyRole();
  }

  function renderVerdict(result) {
    const el = document.getElementById('verdict-banner');
    el.hidden = false;
    const baseline = result.baseline_region;
    if (result.verdict === 'ok') {
      const best = result.comparison[0];
      const allowedCount = result.gate.filter(g => g.allowed).length;
      const blockedCount = result.gate.filter(g => !g.allowed).length;
      el.className = 'verdict ok';
      el.innerHTML = `POLICY PASSED - cleanest allowed region: <code>${escapeHtml(best.region)}</code> (${escapeHtml(best.location)})` +
        `<small>${allowedCount} allowed candidate(s) compared on official grid carbon intensity` +
        (blockedCount ? `; ${blockedCount} blocked candidate(s) excluded by policy` : '') +
        (baseline ? `; baseline ${escapeHtml(baseline)}` : '') + '.</small>';
    } else if (result.verdict === 'blocked') {
      el.className = 'verdict blocked';
      el.innerHTML = 'BLOCKED BY POLICY - no candidate region is allowed' +
        '<small>Every target region failed the policy gate. Greener grid numbers do not override policy. ' +
        'No recommendation was made.</small>';
    } else {
      el.className = 'verdict no_candidates';
      el.innerHTML = 'NOTHING TO REVIEW' +
        '<small>No supported resource change produced a target region. See plan details below.</small>';
    }
  }

  function renderGate(result) {
    document.getElementById('gate-policy-name').textContent =
      `Policy: ${result.policy.name}` +
      (result.policy.residency_label ? ` - data residency: ${result.policy.residency_label}` : '') +
      ` - allowed: ${result.policy.allowed_regions.join(', ')}`;
    const interpBox = document.getElementById('gate-interpreted');
    if (interpBox) {
      if (result.policy.interpreted_from_text) {
        const assumptions = result.policy.interpretation_assumptions || [];
        interpBox.hidden = false;
        interpBox.innerHTML =
          `<strong>Interpreted from plain English by model</strong> and verified against the official region catalog.` +
          (assumptions.length
            ? '<ul>' + assumptions.map(a => `<li>${escapeHtml(a)}</li>`).join('') + '</ul>'
            : '');
      } else {
        interpBox.hidden = true;
      }
    }
    showCard('gate-card', true);
    const list = document.getElementById('gate-list');
    list.innerHTML = '';
    result.gate.forEach(g => {
      const row = document.createElement('div');
      row.className = 'gate-row ' + (g.allowed ? 'allow' : 'block');
      let reasons = g.allowed
        ? 'Passes the allowed-region list' + (result.policy.residency_label ? ' and the residency check' : '') + '.'
        : g.reasons.map(escapeHtml).join('<br>');
      // Demo hook: show the carbon number a blocked region was blocked despite.
      if (!g.allowed && g.carbon) {
        let line = `Grid intensity ${g.carbon.grid_carbon_intensity} gCO2eq/kWh, CFE ${g.carbon.cfe_pct}%`;
        if (g.carbon.delta_pct_vs_baseline !== null && g.carbon.delta_pct_vs_baseline < 0) {
          line += ` - ${Math.abs(g.carbon.delta_pct_vs_baseline).toFixed(1)}% lower than the baseline. Cleaner, and still blocked by policy.`;
        } else {
          line += '. Blocked by policy.';
        }
        reasons += '<br><span class="despite">' + escapeHtml(line) + '</span>';
      }
      row.innerHTML = `<span class="icon">${g.allowed ? '&#10003;' : '&#10007;'}</span>` +
        `<div><div class="region">${escapeHtml(g.region)}</div><div class="reasons">${reasons}</div></div>`;
      list.appendChild(row);
    });
    const unknown = document.getElementById('gate-unknown');
    if (result.unknown_allowed_regions.length) {
      unknown.hidden = false;
      unknown.className = 'note warn';
      unknown.textContent =
        'No official carbon data for: ' + result.unknown_allowed_regions.join(', ') +
        '. These regions passed the gate but are excluded from the comparison instead of being guessed.';
    } else {
      unknown.hidden = true;
    }
  }

  function renderComparison(result) {
    if (!result.comparison.length) { showCard('compare-card', false); return; }
    showCard('compare-card', true);
    const body = document.getElementById('compare-body');
    body.innerHTML = '';
    const max = Math.max(...result.comparison.map(r => r.grid_carbon_intensity));
    result.comparison.forEach((row, i) => {
      const tr = document.createElement('tr');
      if (i === 0) tr.className = 'best';
      let delta = 'n/a';
      let cls = '';
      if (row.is_baseline) { delta = 'baseline'; }
      else if (row.delta_pct_vs_baseline !== null) {
        if (row.delta_pct_vs_baseline === 0) { delta = 'baseline'; }
        else {
          delta = (row.delta_pct_vs_baseline > 0 ? '+' : '') + row.delta_pct_vs_baseline.toFixed(1) + '%';
          cls = row.delta_pct_vs_baseline < 0 ? 'delta-neg' : 'delta-pos';
        }
      }
      const width = Math.max(4, Math.round(row.grid_carbon_intensity / max * 100));
      tr.innerHTML =
        `<td>${escapeHtml(row.region)} <span class="dim">(${escapeHtml(row.location)})</span></td>` +
        `<td>${row.cfe_pct}%</td>` +
        `<td><div class="bar-wrap"><div class="bar" style="width:${width}%"></div></div> ${row.grid_carbon_intensity}</td>` +
        `<td class="${cls}">${delta}</td>` +
        `<td>${row.low_carbon ? '<span class="leaf">low CO2</span>' : ''}</td>`;
      body.appendChild(tr);
    });
    const meta = result.carbon_data;
    document.getElementById('compare-source').textContent =
      `Source: ${meta.source} - ${meta.data_year} ${meta.data_kind}, retrieved ${meta.retrieved_at}. ` +
      'Grid metrics, not measured workload emissions. ' + meta.source_url;
  }

  function renderReview(result) {
    const review = result.review;
    if (!review) { showCard('review-card', false); return; }
    showCard('review-card', true);
    const badge = document.getElementById('review-badge');
    const labels = {
      gemini: review.model ? `Gemini: ${review.model} (live)` : 'Gemini (live)',
      offline: 'offline template - not model output',
      rejected: 'model output rejected - code-verified fallback',
      error: 'model call failed - code-verified fallback',
    };
    badge.className = 'badge ' + review.status;
    badge.textContent = labels[review.status] || review.status;
    const note = document.getElementById('review-note');
    if (review.note) { note.hidden = false; note.className = 'note warn'; note.textContent = review.note; }
    else { note.hidden = true; }
    document.getElementById('review-comment').textContent = review.comment_markdown;
  }

  function applyRole() {
    const role = ROLES[roleSelect.value] || ROLES.operator;
    roleBanner.textContent = role.blurb;
    roleBanner.dataset.role = roleSelect.value;
    runBtn.disabled = !role.canRun;
    runBtn.textContent = role.canRun ? 'Run review' : 'Read-only view';
    // Cards a role does not need are hidden, not reordered: the same review
    // stays in the same place for everyone who can see it.
    ALL_CARDS.forEach(id => {
      const el = document.getElementById(id);
      if (!el) return;
      el.dataset.roleHidden = role.cards.includes(id) ? '' : '1';
    });
    if (lastResult) {
      renderAll(lastResult);
      showState('results');
    } else if (!role.canRun) {
      // Read-only auditor view with no active review run yet: show the
      // methodology and recorded history instead of a dead-end empty prompt.
      document.getElementById('verdict-banner').hidden = true;
      ['impact-card', 'gate-card', 'compare-card', 'review-card', 'trace-card', 'resources-card'].forEach(
        id => showCard(id, false)
      );
      renderMethodology(null);
      renderHistory();
      document.getElementById('elapsed').textContent = '';
      showState('results');
    } else {
      renderMethodology(null);
      renderHistory();
      showState('empty');
    }
  }

  function showCard(id, visible) {
    const el = document.getElementById(id);
    if (!el) return;
    el.hidden = !visible || el.dataset.roleHidden === '1';
  }

  function renderMethodology(result) {
    const body = document.getElementById('method-body');
    const meta = (result && result.carbon_data) || null;
    const assumptions = (result && result.impact && result.impact.assumptions) || [];
    const rows = [];

    rows.push('<h4>Where the carbon numbers come from</h4>');
    if (meta) {
      rows.push('<ul class="assumptions">' +
        `<li>${escapeHtml(meta.source)} &mdash; ${meta.data_year} ${escapeHtml(meta.data_kind)}, ` +
        `retrieved ${escapeHtml(meta.retrieved_at)}.</li>` +
        `<li>CFE %: ${escapeHtml(meta.units.cfe_pct)}</li>` +
        `<li>Grid intensity: ${escapeHtml(meta.units.grid_carbon_intensity)}</li>` +
        `<li>Low-carbon badge: ${escapeHtml(meta.low_carbon_rule)}</li>` +
        '</ul>' +
        `<p class="note"><a href="${escapeHtml(meta.source_url)}" target="_blank" rel="noopener">` +
        `${escapeHtml(meta.source_url)}</a></p>`);
    } else {
      rows.push('<p class="note">Run a review to load the bundled data source.</p>');
    }

    if (assumptions.length) {
      rows.push('<h4>How the kgCO2eq estimate is built</h4>');
      rows.push('<ul class="assumptions">' +
        assumptions.map(a => `<li>${escapeHtml(a)}</li>`).join('') + '</ul>');
    }

    rows.push('<h4>What this is not</h4>');
    rows.push('<ul class="assumptions">' +
      '<li>Not a measurement. The kgCO2eq figure is derived from vCPU count, ' +
      'not from metered power draw or a cloud bill.</li>' +
      '<li>Not a compliance certification. The policy you supply is verified as ' +
      'written; nothing here certifies that the policy itself is correct or legal.</li>' +
      '<li>Not real-time. Grid intensity is an annual average, not the marginal ' +
      'intensity at the hour a workload runs.</li>' +
      '<li>Not an actuator. Output is advisory; it never changes infrastructure ' +
      'or posts to a real pull request.</li>' +
      '<li>Reviews <code>google_compute_instance</code> only. Other resource types ' +
      'are reported as unreviewed rather than silently skipped.</li>' +
      '</ul>');
    body.innerHTML = rows.join('');
    showCard('method-card', true);
  }

  function renderHistory() {
    const entries = readHistory();
    const list = document.getElementById('history-list');
    document.getElementById('history-chip').textContent =
      entries.length ? `${entries.length} recorded` : 'none yet';
    if (!entries.length) {
      list.innerHTML = '<li class="history-empty">No reviews recorded in this browser yet. ' +
        'Each run appends one entry.</li>';
    } else {
      list.innerHTML = entries.map(e => {
        const when = new Date(e.reviewed_at);
        const stamp = isNaN(when) ? e.reviewed_at : when.toLocaleString();
        const verdictCls = e.verdict === 'ok' ? 'ok' : (e.verdict === 'blocked' ? 'blocked' : 'none');
        const impact = e.delta_kg === null || e.delta_kg === undefined
          ? ''
          : `<span class="hist-impact ${e.delta_kg < 0 ? 'delta-neg' : 'delta-pos'}">` +
            `${e.delta_kg > 0 ? '+' : ''}${Math.round(e.delta_kg)} kgCO2eq/yr</span>`;
        return '<li class="history-row">' +
          `<div class="hist-head"><span class="hist-verdict ${verdictCls}">` +
          `${escapeHtml(e.verdict)}</span>` +
          `<span class="hist-policy">${escapeHtml(e.policy)}</span>` +
          `<span class="hist-when">${escapeHtml(stamp)}</span></div>` +
          `<div class="hist-body">${escapeHtml(e.summary)} ${impact}</div>` +
          `<div class="hist-chain">decided by code &middot; comment: ` +
          `${escapeHtml(e.review_status)}${e.model ? ' (' + escapeHtml(e.model) + ')' : ''}` +
          `${e.interpreted ? ' &middot; policy read from prose by model' : ''}</div>` +
        '</li>';
      }).join('');
    }
    showCard('history-card', true);
  }

  function recordReview(result) {
    const gateBlocked = result.gate.filter(g => !g.allowed).map(g => g.region);
    const best = result.comparison.length ? result.comparison[0].region : null;
    let summary;
    if (result.verdict === 'ok') {
      summary = `Recommended ${best}.`;
    } else if (result.verdict === 'blocked') {
      summary = `Blocked: ${gateBlocked.join(', ') || 'no allowed region'}.`;
    } else {
      summary = 'No reviewable target region in the plan.';
    }
    appendHistory({
      reviewed_at: result.reviewed_at || new Date().toISOString(),
      policy: result.policy.name,
      verdict: result.verdict,
      summary,
      delta_kg: result.impact ? result.impact.total_delta_kg_co2e : null,
      review_status: result.review ? result.review.status : 'not written',
      model: result.review ? result.review.model : null,
      interpreted: !!result.policy.interpreted_from_text,
    });
  }

  function renderAll(result) {
    renderVerdict(result);
    renderImpact(result);
    renderGate(result);
    renderComparison(result);
    renderReview(result);
    renderResources(result);
    renderTrace(result);
    renderMethodology(result);
    renderHistory();
  }

  function renderImpact(result) {
    const card = document.getElementById('impact-card');
    const imp = result.impact;
    const nothing = !imp || (imp.total_target_kg_co2e === null &&
      imp.total_baseline_kg_co2e === null && !imp.total_new_kg_co2e);
    if (nothing) {
      showCard('impact-card', false);
      return;
    }
    showCard('impact-card', true);
    const label = document.getElementById('impact-label');
    const figure = document.getElementById('impact-figure');
    const sub = document.getElementById('impact-sub');

    if (imp.total_delta_kg_co2e !== null) {
      const saving = imp.total_delta_kg_co2e < 0;
      card.className = 'card impact-card ' + (saving ? 'saving' : 'increase');
      label.textContent = saving ? 'Estimated annual saving' : 'Estimated annual increase';
      figure.textContent = fmtKg(Math.abs(imp.total_delta_kg_co2e)) + '/year';
      sub.textContent =
        `${fmtKg(imp.total_baseline_kg_co2e)} today → ` +
        `${fmtKg(imp.total_target_kg_co2e)} after the change, across ` +
        `${imp.migrated_count} migrated resource(s).` +
        (imp.total_new_kg_co2e
          ? ` ${imp.created_count} new resource(s) add ${fmtKg(imp.total_new_kg_co2e)}/year ` +
            'on top - an addition, not netted off.'
          : '') +
        ' Estimated from vCPU count - not a measurement.';
    } else if (imp.total_baseline_kg_co2e !== null) {
      card.className = 'card impact-card neutral';
      label.textContent = 'Current annual footprint';
      figure.textContent = fmtKg(imp.total_baseline_kg_co2e) + '/year';
      sub.textContent =
        'No policy-approved destination, so no post-change figure is shown. ' +
        'A blocked region is never costed as a saving.';
    } else if (imp.total_new_kg_co2e) {
      card.className = 'card impact-card increase';
      label.textContent = 'Estimated footprint of new resources';
      figure.textContent = fmtKg(imp.total_new_kg_co2e) + '/year';
      sub.textContent =
        `${imp.created_count} resource(s) are newly created, so there is nothing to ` +
        'compare against. Estimated from vCPU count - not a measurement.';
    } else {
      card.className = 'card impact-card neutral';
      label.textContent = 'No estimate';
      figure.textContent = '—';
      sub.textContent = 'No recognizable machine shape in this plan; nothing was guessed.';
    }

    const list = document.getElementById('impact-assumptions');
    list.innerHTML = '';
    imp.assumptions.forEach(a => {
      const li = document.createElement('li');
      li.textContent = a;
      list.appendChild(li);
    });

    const rows = document.getElementById('impact-rows');
    rows.innerHTML = '';
    imp.resources.forEach(r => {
      const tr = document.createElement('tr');
      let figureCell;
      if (r.delta_kg_co2e !== null) {
        figureCell = `${r.baseline_kg_co2e} → ${r.target_kg_co2e} kgCO2eq/yr ` +
          `<span class="${r.delta_kg_co2e < 0 ? 'delta-neg' : 'delta-pos'}">` +
          `(${r.delta_kg_co2e > 0 ? '+' : ''}${r.delta_kg_co2e})</span>`;
      } else if (r.target_kg_co2e !== null) {
        figureCell = `<span class="delta-pos">+${r.target_kg_co2e} kgCO2eq/yr</span>` +
          `<div class="dim">new resource</div>`;
      } else {
        figureCell = `<span class="dim">${escapeHtml(r.note || 'no estimate')}</span>`;
      }
      tr.innerHTML =
        `<td>${escapeHtml(r.address)}<div class="dim">` +
        `${escapeHtml(r.machine_type || 'unknown shape')}` +
        `${r.vcpus !== null ? ' · ' + r.vcpus + ' vCPU' : ''}</div></td>` +
        `<td>${figureCell}</td>`;
      rows.appendChild(tr);
    });
  }

  function renderTrace(result) {
    const steps = result.agent_trace || [];
    if (!steps.length) { showCard('trace-card', false); return; }
    showCard('trace-card', true);
    const ran = steps.filter(s => s.status !== 'skipped').length;
    const modelRan = steps.filter(s => s.engine === 'gemini' && s.status !== 'skipped').length;
    document.getElementById('trace-chip').textContent =
      `${ran} of ${steps.length} ran · ${modelRan} used a model`;
    const list = document.getElementById('trace-list');
    list.innerHTML = '';
    steps.forEach(s => {
      const li = document.createElement('li');
      li.className = 'trace-step ' + s.status;
      const engine = s.engine === 'gemini'
        ? '<span class="engine model">model</span>'
        : '<span class="engine code">code</span>';
      li.innerHTML =
        `<div class="trace-head">${engine}` +
        `<span class="trace-name">${escapeHtml(s.name)}</span>` +
        `<span class="trace-status ${s.status}">${s.status}</span>` +
        (s.status !== 'skipped' ? `<span class="trace-ms">${s.duration_ms} ms</span>` : '') +
        `</div>` +
        `<div class="trace-summary">${escapeHtml(s.summary)}</div>` +
        `<div class="trace-role">${escapeHtml(s.role)}</div>`;
      list.appendChild(li);
    });
  }

  function renderResources(result) {
    const r = result.resources;
    if (!r.supported_changes.length && !r.unsupported.length && !r.issues.length) {
      showCard('resources-card', false); return;
    }
    showCard('resources-card', true);
    const list = document.getElementById('resources-list');
    list.innerHTML = '';
    r.supported_changes.forEach(c => {
      const div = document.createElement('div');
      div.className = 'res-row';
      div.innerHTML = `<span class="kind">${escapeHtml(c.actions.join('+'))}</span> ${escapeHtml(c.address)}` +
        ` <span class="dim">${escapeHtml(c.before_region || 'new')} &rarr; ${escapeHtml(c.after_region || 'deleted')}` +
        (c.machine_type ? ` · ${escapeHtml(c.machine_type)}` : '') + '</span>';
      list.appendChild(div);
    });
    r.unsupported.forEach(u => {
      const div = document.createElement('div');
      div.className = 'res-row unsupported';
      div.textContent = `unsupported resource type: ${u.address} (${u.type}) - reported, not reviewed`;
      list.appendChild(div);
    });
    r.issues.forEach(msg => {
      const div = document.createElement('div');
      div.className = 'res-row issue';
      div.textContent = 'issue: ' + msg;
      list.appendChild(div);
    });
  }

  async function runReview() {
    inputError.hidden = true;
    let plan, policy = null, text = null;
    try { plan = JSON.parse(planEditor.value); }
    catch (e) { inputError.textContent = 'Plan is not valid JSON: ' + e.message; inputError.hidden = false; return; }
    if (policyMode === 'text') {
      text = policyText.value.trim();
      if (!text) { inputError.textContent = 'Write the policy in plain English, or switch to structured JSON.'; inputError.hidden = false; return; }
    } else {
      try { policy = JSON.parse(policyEditor.value); }
      catch (e) { inputError.textContent = 'Policy is not valid JSON: ' + e.message; inputError.hidden = false; return; }
    }

    runBtn.disabled = true;
    showState('loading');
    try {
      const res = await fetch('/api/review', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(policyMode === 'text' ? { plan, policy_text: text } : { plan, policy }),
      });
      const body = await res.json();
      if (!res.ok) {
        const msg = body.detail || res.statusText;
        inputError.textContent = msg;
        inputError.hidden = false;
        showState('empty');
        emptyState.querySelector('p').textContent = 'Review rejected: ' + msg;
        return;
      }
      lastResult = body;
      recordReview(body);
      renderAll(body);
      document.getElementById('elapsed').textContent = `Completed in ${body.elapsed_ms} ms.`;
      showState('results');
    } catch (e) {
      inputError.textContent = 'Request failed: ' + e.message;
      inputError.hidden = false;
      showState('empty');
      emptyState.querySelector('p').textContent = 'Request failed: ' + e.message;
    } finally {
      const role = ROLES[roleSelect.value] || ROLES.operator;
      runBtn.disabled = !role.canRun;
    }
  }

  modeJsonBtn.addEventListener('click', () => setPolicyMode('json'));
  modeTextBtn.addEventListener('click', () => setPolicyMode('text'));
  document.getElementById('impact-toggle').addEventListener('click', (e) => {
    const detail = document.getElementById('impact-detail');
    detail.hidden = !detail.hidden;
    e.target.setAttribute('aria-expanded', String(!detail.hidden));
  });
  const copyBtn = document.getElementById('copy-comment-btn');
  if (copyBtn) {
    copyBtn.addEventListener('click', async () => {
      const text = document.getElementById('review-comment').textContent || '';
      try {
        await navigator.clipboard.writeText(text);
        copyBtn.textContent = 'Copied!';
        setTimeout(() => { copyBtn.textContent = 'Copy markdown'; }, 1500);
      } catch (e) {
        copyBtn.textContent = 'Copy failed';
        setTimeout(() => { copyBtn.textContent = 'Copy markdown'; }, 1500);
      }
    });
  }
  roleSelect.addEventListener('change', applyRole);
  document.getElementById('history-clear').addEventListener('click', () => {
    try { localStorage.removeItem(HISTORY_KEY); } catch (e) { /* ignore */ }
    renderHistory();
  });
  runBtn.addEventListener('click', runReview);
  applyRole();
  loadMeta().then(() => loadSamples()).then(() => {
    // ?autorun=N loads sample N (1-based) and runs the review once - handy for
    // scripted checks and demo recording.
    const m = new URLSearchParams(location.search).get('autorun');
    if (m) {
      const i = Math.min(Math.max(parseInt(m, 10) || 1, 1), samples.length) - 1;
      selectSample(i, sampleBar.children[i]);
      runReview();
    }
  }).catch(e => {
    sampleDesc.textContent = 'Could not load samples: ' + e.message;
  });
})();
