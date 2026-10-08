// Dashboard "Positions & P&L" panel. Reads state.accountSummary / positionDisplayRows() (kept current by
// account_update, which the server sends whatever tab is open), so it needs no message of its own.
// Drawn through renderWhenVisible, i.e. only while the Dashboard is visible. Text only (textContent):
// nothing from the feed is ever parsed as HTML.
function _dashKpi(id, value, signed) {
    const el = document.getElementById(id);
    if (!el) return;
    const text = fmtCurrency(value);
    if (el.textContent !== text) el.textContent = text;
    const cls = 'kpi-value' + (signed && isFinite(value) && value > 0 ? ' pos' : (signed && isFinite(value) && value < 0 ? ' neg' : ''));
    if (el.className !== cls) el.className = cls;
}

function _describePosition(p) {
    const c = p.contract || {};
    if (c.secType === 'OPT') {
        const strike = c.strike !== null && c.strike !== undefined ? String(c.strike) : '';
        return `${c.symbol || '?'} ${fmtExpiry(c.expiry)} ${strike}${c.right || ''}`.trim();
    }
    return c.symbol || c.localSymbol || '?';
}

function _dashCell(text, cls) {
    const td = document.createElement('td');
    td.textContent = text;
    if (cls) td.className = cls;
    return td;
}

function _dashPositionRow(p, label, cls) {
    const tr = document.createElement('tr');
    if (cls) tr.className = cls;
    const mark = (p.marketPrice === null || p.marketPrice === undefined || !isFinite(p.marketPrice)) ? '-' : Number(p.marketPrice).toFixed(2);
    tr.appendChild(label instanceof Node ? label : _dashCell(label));
    tr.appendChild(_dashCell(String(p.position), 'num'));
    tr.appendChild(_dashCell(mark, 'num'));
    tr.appendChild(_dashCell(fmtCurrencyFull(p.unrealizedPNL), 'num ' + pnlClass(p.unrealizedPNL)));
    return tr;
}

// A spread: one row (toggle + label, per-spread qty and mark, summed P&L); its legs follow when expanded.
function _dashSpreadRows(r, frag) {
    const open = state.expandedSpreads.has(r.id);
    const td = document.createElement('td');
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'spread-toggle';
    btn.textContent = open ? '▾' : '▸';
    btn.title = (open ? 'Hide' : 'Show') + ' the legs';
    btn.setAttribute('aria-expanded', String(open));
    btn.addEventListener('click', () => toggleSpread(r.id));
    td.appendChild(btn);
    td.appendChild(document.createTextNode(' ' + (r.label || 'Spread')));
    frag.appendChild(_dashPositionRow(r, td, 'spread-row'));
    if (open) for (const leg of r.legs || []) frag.appendChild(_dashPositionRow(leg, _describePosition(leg), 'spread-leg'));
}

function renderDashPositions() {
    const body = document.getElementById('dashPositionsBody');
    if (!body) return;
    const s = state.accountSummary || {};
    _dashKpi('dashNetLiq', s.NetLiquidation, false);
    _dashKpi('dashUnPnl', s.UnrealizedPnL, true);
    _dashKpi('dashRePnl', s.RealizedPnL, true);

    const rows = positionDisplayRows();
    const frag = document.createDocumentFragment();
    if (rows.length === 0) {
        const tr = document.createElement('tr');
        const td = _dashCell('No open positions', 'acct-empty');
        td.colSpan = 4;
        tr.appendChild(td);
        frag.appendChild(tr);
    }
    for (const r of rows) {
        if (r.kind === 'spread') _dashSpreadRows(r, frag);
        else frag.appendChild(_dashPositionRow(r, _describePosition(r)));
    }
    body.replaceChildren(frag);
}
