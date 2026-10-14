// InfoGrep's side panel. It reads the form fields of the page beside it (page.js), has the
// local InfoGrep server work out what goes in them (a DeepSeek Harness agent searches the
// user's files there: infogrep/fill.py), and types the values in. It never presses a
// button or submits a form; the user checks the page and does that.
//
// A form can change as it is filled in: a province brings its cities, a "yes" brings the
// fields that ask for details. So a fill goes in rounds. After the values are in, a list
// that filled in late gets its value again, and the fields that came up, or now offer
// other choices, go back to the agent in the same conversation.
//
// Text from the page is only ever put on screen as text (textContent), never as HTML.

const DEFAULT_SERVER = 'http://127.0.0.1:7421';
const MAX_FIELDS = 200;  // what the server takes: a longer form has its first ones filled in
const ROUNDS = 3;        // the first values, and twice more for what they bring onto the page
const $ = id => document.getElementById(id);
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

let server = DEFAULT_SERVER;
let windowId = null;  // the window this panel sits in; its active tab is "the page"
let page = null;      // the last look at it: {tabId, title, url, fields, where, keys}
let run = null;       // the fill on its way (see fillPage)
let session = '';     // the agent's conversation, which the next fill goes on with
let savedDir = '';

// Why a value did not go in, as page.js reports it, and as the panel decides.
const WHY = {
  kept: 'Already filled in, so left as it is. Tick “Replace what is already filled in” to use this.',
  unsourced: 'Not typed in: the file it names does not have this word for word, so the agent ' +
    'may have written it itself. Use it yourself if it is right.',
  option: 'Not one of the choices this field offers.',
  refused: 'The field did not take it.',
  locked: 'The field takes no input right now.',
  gone: 'The field is no longer on the page.',
};

// -- small things -----------------------------------------------------------------

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function say(text, kind = '') {
  const meta = $('meta');
  meta.className = 'meta ' + kind;
  meta.replaceChildren();
  if (kind === 'busy') meta.append(el('span', 'spin'));
  meta.append(text);
}

const nameOf = field => field.label || field.hint || field.name || field.id;
const shown = value => (typeof value === 'boolean' ? (value ? 'ticked' : 'not ticked')
  : Array.isArray(value) ? value.join(' / ') : String(value));
const number = id => Number(id.slice(1));
// What the agent was shown of a field that could change: its kind, choices and hint.
const signature = field => JSON.stringify([field.kind, field.options || [], field.hint || '']);

async function api(path, options = {}) {
  const response = await fetch(server + path, { signal: AbortSignal.timeout(20000), ...options });
  return response.json();
}

function remember(values) {
  chrome.storage.local.set(values).catch(() => {});
}

// Run `func` in a tab's frames (or the documents named), with page.js loaded there first.
async function inPage(target, func, args = []) {
  await chrome.scripting.executeScript({ target, files: ['page.js'] });
  return chrome.scripting.executeScript({ target, func, args });
}

// Let the page finish what the values put in set off: options arriving, fields appearing.
async function settle(tabId) {
  try {
    await inPage({ tabId, allFrames: true }, () => globalThis.__infogrepFill.settle(400, 4000, 800));
  } catch {
    // a frame that has gone has nothing left to settle
  }
}

// What of an address the agent is told: no query string or fragment, which carry tokens.
function shortUrl(url) {
  try {
    const { origin, pathname } = new URL(url);
    return origin + pathname;
  } catch {
    return '';
  }
}

// -- the server -------------------------------------------------------------------

function trouble(text) {
  $('troubletext').textContent = text;
  $('trouble').hidden = false;
  $('main').hidden = true;
  return false;
}

// What is remembered about the user, under its tick box: how much, and where to see it.
async function showMemory() {
  let about;
  try {
    about = await api('/api/fill/memory');
  } catch {
    return;
  }
  const box = $('facts');
  box.replaceChildren(about.facts
    ? `${about.facts} fact${about.facts > 1 ? 's' : ''} remembered`
    : 'Nothing remembered yet: what the agent finds is kept for the next form.');
  if (about.facts && about.url) {
    const open = el('a', '', 'see them in Obsidian');
    open.href = about.url;  // Chrome asks before it opens the app
    box.append(' · ', open);
  } else if (about.facts) {  // no vault: a plain note, for any text editor
    const show = el('a', '', 'show the note');
    show.href = '#';
    show.title = about.path;
    show.addEventListener('click', event => {
      event.preventDefault();
      reveal(about.path);
    });
    box.append(' · ', show);
  }
  box.hidden = false;
}

// Find the InfoGrep server and the folders it searches. Returns whether filling can go ahead.
async function connect() {
  let info;
  try {
    info = await api('/api/indexes');
  } catch {
    return trouble(`InfoGrep isn't answering at ${server}. Open the InfoGrep app, or run ` +
      '“infogrep serve”, then retry.');
  }
  if (!('fill' in info)) {
    return trouble('This InfoGrep server is too old to fill in forms. Update InfoGrep and ' +
      'restart its server.');
  }
  if (!info.fill) {
    return trouble('The InfoGrep server did not find DeepSeek Harness (dsh), the agent that ' +
      'works out the values. Install it (see “Set up question answering” in InfoGrep’s ' +
      'README), then retry.');
  }
  const folders = (info.indexes || []).filter(index => index.indexed);
  if (!folders.length) return trouble('InfoGrep has no indexed folder yet. Index one, then retry.');
  const select = $('dir');
  const keep = select.options.length ? select.value : savedDir;
  select.replaceChildren(new Option('in all indexed folders', ''),
    ...folders.map(index => new Option(`in ${index.name}`, index.dir)));
  select.value = folders.some(index => index.dir === keep) ? keep : '';
  $('trouble').hidden = true;
  $('main').hidden = false;
  showMemory();
  return true;
}

// -- the page ---------------------------------------------------------------------

// The fields of a tab, in every frame of it; null for a page extensions may not read.
// `keys` gives each element the field id it had in an earlier look, and new ones the next.
async function look(tab, keys = new Map()) {
  let frames;
  try {
    frames = await inPage({ tabId: tab.id, allFrames: true },
      () => globalThis.__infogrepFill.scan());
  } catch {
    return null;
  }
  const fields = [];
  const where = new Map();  // field id -> {documentId, n}: the frame and element it is
  for (const frame of frames.sort((a, b) => a.frameId - b.frameId)) {
    for (const { n, ...field } of frame.result || []) {
      const key = `${frame.documentId}:${n}`;
      if (!keys.has(key)) keys.set(key, `f${keys.size + 1}`);
      where.set(keys.get(key), { documentId: frame.documentId, n });
      fields.push({ id: keys.get(key), ...field });
    }
  }
  return { tabId: tab.id, title: tab.title || '', url: shortUrl(tab.url), fields, where, keys };
}

// Look at the page beside the panel, and say what is on it.
async function scan() {
  const [tab] = await chrome.tabs.query({ active: true, windowId });
  page = null;
  $('fill').disabled = true;
  $('pagetitle').textContent = tab ? tab.title || tab.url || 'This tab' : 'No tab';
  $('pagetitle').title = tab?.url || '';
  if (!tab) return null;
  page = await look(tab);
  if (!page) {
    $('count').textContent = 'Extensions can’t read this page (the browser’s own pages and ' +
      'the Web Store are closed to them).';
    return null;
  }
  const { fields } = page;
  const filled = fields.filter(field => 'value' in field).length;
  $('count').textContent = !fields.length ? 'No form fields on this page.'
    : `${fields.length} field${fields.length > 1 ? 's' : ''}` +
      (filled ? `, ${filled} already filled in` : '') +
      (fields.length > MAX_FIELDS ? ` (the first ${MAX_FIELDS} get filled in)` : '');
  $('fill').disabled = !fields.length || !!run;
  return page;
}

let rescanTimer = 0;
function rescanSoon() {
  clearTimeout(rescanTimer);
  rescanTimer = setTimeout(() => run || $('main').hidden || scan(), 300);
}

async function showField(mine, id) {
  const { documentId, n } = mine.where.get(id);
  try {
    await chrome.tabs.update(mine.tabId, { active: true });
    await inPage({ tabId: mine.tabId, documentIds: [documentId] },
      number => globalThis.__infogrepFill.show(number), [n]);
  } catch {
    say('That page has changed since: the field is not there any more.', 'err');
  }
}

async function reveal(path) {
  try {
    const result = await api('/api/open?path=' + encodeURIComponent(path));
    if (!result.ok) say(`Could not show the file: ${result.error || 'error'}`, 'err');
  } catch (error) {
    say(`Could not show the file: ${error.message}`, 'err');
  }
}

// -- filling in -------------------------------------------------------------------

function busy(on) {
  $('fill').hidden = on;
  $('stop').hidden = !on;
  for (const id of ['note', 'dir', 'overwrite', 'memory', 'rescan']) $(id).disabled = on;
  $('memo').hidden = on || !session;
  if (on) {
    $('steps').replaceChildren();
    $('results').replaceChildren();
  }
}

async function fillPage() {
  if (run || !(await connect())) return;
  const first = await scan();
  if (!first || !first.fields.length) return;
  const mine = run = {
    tabId: first.tabId, title: first.title, url: first.url, keys: first.keys,
    where: new Map(first.where), fields: new Map(first.fields.map(f => [f.id, f])),
    sent: new Map(),  // field id -> its signature when the agent was last shown it
    rows: new Map(),  // field id -> {field, value, source, error, offered}
    notes: [], round: 0, cut: 0, learned: 0, began: Date.now(), stopped: false,
  };
  busy(true);
  let failure = '';
  try {
    let fields = first.fields.slice(0, MAX_FIELDS);
    while (fields.length) {
      mine.round++;
      const reply = await consult(mine, fields);
      if (reply.note) mine.notes.push(reply.note);
      await putValues(mine, reply.values || []);
      if (mine.round === ROUNDS || mine.stopped) break;
      fields = await afterwards(mine);
    }
    if (mine.stopped) failure = 'Stopped.';
  } catch (error) {
    failure = error.message;
    session = '';  // the conversation may be what went wrong: the next fill starts afresh
  } finally {
    if (run === mine) run = null;
    busy(false);
  }
  report(mine, failure);
}

// One round with the agent: show it the fields, follow its steps, and return its reply.
async function consult(mine, fields) {
  const query = new URLSearchParams($('dir').value ? { dir: $('dir').value } : { scope: 'all' });
  if (session) query.set('session', session);
  for (const field of fields) mine.sent.set(field.id, signature(field));
  const round = mine.round > 1 ? `Round ${mine.round} · ` : '';
  say(round + (mine.round > 1 ? 'Asking about what the page added…' : 'Starting the agent…'), 'busy');
  const lost = error => new Error(`InfoGrep stopped answering (${error.message}).`);
  let started;
  try {
    started = await api('/api/fill?' + query, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        page: { title: mine.title, url: mine.url },
        fields,
        note: $('note').value.trim(),
        changed: mine.round > 1,  // the next round of the same form, in the same conversation
        memory: $('memory').checked,
      }),
    });
  } catch (error) {
    throw lost(error);
  }
  if (!started.ok) throw new Error(started.error || 'InfoGrep could not start the agent.');
  // A form too long for one round: the agent was shown its first fields only.
  const taken = started.fields ?? fields.length;
  if (mine.round === 1 && taken < mine.fields.size) mine.cut = taken;
  mine.job = started.id;
  for (;;) {
    if (mine.stopped) throw new Error('Stopped.');
    let state;
    try {
      state = await api(`/api/fill?id=${started.id}`);
    } catch (error) {
      throw lost(error);
    }
    const steps = state.steps || [];
    $('steps').replaceChildren(...steps.slice(-8).map(step => el('div', '', step)));
    if (state.status === 'running') {
      const seconds = Math.round((Date.now() - mine.began) / 1000);
      say(`${round}Looking it up in your files · ${seconds} s`, 'busy');
      await wait(800);
      continue;
    }
    if (state.status === 'done') {
      session = state.session || session;
      mine.learned += state.learned || 0;
      return state;
    }
    throw new Error(state.status === 'cancelled' ? 'Stopped.'
      : state.status === 'gone' ? 'Another fill took over from this one.'
        : state.error || state.status);
  }
}

// Take the agent's values. A field already filled in keeps its value unless the user said
// to replace it, and prose that the file it came from does not have word for word is
// shown, not typed in.
async function putValues(mine, values) {
  const overwrite = $('overwrite').checked;
  const typed = [];
  for (const { id, value, source, copied, remembered } of values) {
    const field = mine.fields.get(id);
    if (!field) continue;
    const row = { field, value, source, remembered, error: '' };
    mine.rows.set(id, row);
    if ('value' in field && !overwrite) row.error = 'kept';
    else if (field.kind === 'richtext' && !copied) row.error = 'unsourced';
    else typed.push(row);
  }
  await typeIn(mine, typed);
}

// Type rows' values into the page, a batch per frame in page order. Each row learns how it
// went, and for a value a list did not have, what the list offered then.
async function typeIn(mine, rows) {
  if (!rows.length) return;
  say(`Filling in ${rows.length} field${rows.length > 1 ? 's' : ''}…`, 'busy');
  const batches = new Map();  // documentId -> [{n, row}]
  for (const row of [...rows].sort((a, b) => number(a.field.id) - number(b.field.id))) {
    const { documentId, n } = mine.where.get(row.field.id);
    if (!batches.has(documentId)) batches.set(documentId, []);
    batches.get(documentId).push({ n, row });
  }
  for (const [documentId, batch] of batches) {
    let results = [];
    try {
      [{ result: results }] = await inPage({ tabId: mine.tabId, documentIds: [documentId] },
        items => globalThis.__infogrepFill.fill(items),
        [batch.map(({ n, row }) => ({ n, value: row.value }))]);
    } catch {
      // that frame has gone (the tab moved on): each of its values says so below
    }
    batch.forEach(({ row }, i) => {
      row.error = results?.[i]?.error ?? 'gone';
      row.offered = results?.[i]?.options?.join('\n');
    });
  }
}

// Look again at the page being filled in, and keep `mine` up to date with it.
async function rescan(mine) {
  let now = null;
  try {
    now = await look(await chrome.tabs.get(mine.tabId), mine.keys);
  } catch {
    // the tab was closed
  }
  if (!now) return [];
  for (const [id, at] of now.where) mine.where.set(id, at);
  for (const field of now.fields) mine.fields.set(field.id, field);
  return now.fields;
}

// What the values put in changed. A list that filled in late (the cities of the province
// just chosen) gets its value again, as often as that brings more; then the fields that
// came up, or now offer other choices, are returned for the agent's next round.
async function afterwards(mine) {
  say('Seeing what that changed on the page…', 'busy');
  let fields = [];
  for (let pass = 0; pass < 4 && !mine.stopped; pass++) {
    await settle(mine.tabId);
    fields = await rescan(mine);
    const offers = new Map(fields.map(field => [field.id, (field.options || []).join('\n')]));
    const again = [...mine.rows.values()].filter(row => row.error === 'option' &&
      row.offered !== undefined && offers.has(row.field.id) &&
      offers.get(row.field.id) !== row.offered);
    if (!again.length) break;
    await typeIn(mine, again);
  }
  return fields.filter(field => {
    const row = mine.rows.get(field.id);
    if ((row && !row.error) || 'value' in field) return false;  // in already
    const was = mine.sent.get(field.id);
    if (was === undefined) return true;  // new on the page
    if (signature(field) === was) return false;  // the agent has seen it as it is
    if (row?.error === 'option') field.tried = shown(row.value);
    return true;
  }).slice(0, MAX_FIELDS);
}

async function stop() {
  if (!run) return;
  run.stopped = true;
  try {
    await api('/api/fill/cancel', { method: 'POST' });  // the round in progress ends with it
  } catch {
    // the server is gone, and the agent with it
  }
}

function rowOf(mine, { field, value, source, remembered, error }) {
  const prose = field.kind === 'richtext' && !error;
  const row = el('div', 'row ' + (!error ? (prose ? 'ok prose' : 'ok') : error === 'kept' ? 'kept' : 'bad'));
  row.title = 'Show this field on the page';
  const top = el('div', 'top');
  top.append(el('span', 'tick', !error ? '✓' : error === 'kept' ? '–' : '!'),
    el('span', 'label', nameOf(field)));
  row.append(top, el('div', 'value', shown(value)));
  if (prose) {
    row.append(el('div', 'why', 'Copied word for word from the file below. Read it on the ' +
      'page before you submit.'));
  } else if (error) {
    row.append(el('div', 'why', WHY[error] || error));
  }
  if (source) {
    const from = el('div', 'src', (remembered ? 'remembered, from ' : 'from ') +
      source.split('/').pop());
    from.title = source;
    if (source.startsWith('/')) {
      from.classList.add('link');
      from.title += '\nClick to show it in its folder';
      from.addEventListener('click', event => {
        event.stopPropagation();
        reveal(source);
      });
    }
    row.append(from);
  }
  row.addEventListener('click', () => showField(mine, field.id));
  return row;
}

function report(mine, failure) {
  const rows = [...mine.rows.values()].sort((a, b) => number(a.field.id) - number(b.field.id));
  const put = rows.filter(row => !row.error).length;
  const total = mine.fields.size;
  const rounds = mine.round > 1 ? ` in ${mine.round} rounds` : '';
  if (failure) say(put ? `${failure} ${put} field${put > 1 ? 's were' : ' was'} filled in before that.` : failure,
    failure === 'Stopped.' ? '' : 'err');
  else {
    say(`Filled in ${put} of ${total} field${total > 1 ? 's' : ''}${rounds}` +
      (mine.cut ? ` (the agent was shown the first ${mine.cut}).` : '.'), put ? 'ok' : '');
  }
  const results = $('results');
  results.replaceChildren();
  for (const note of mine.notes) results.append(el('div', 'note', note));
  for (const row of rows) results.append(rowOf(mine, row));
  const empty = [...mine.fields.values()]
    .filter(field => !mine.rows.has(field.id) && !('value' in field));
  if (empty.length && !failure) {
    results.append(el('div', 'left',
      `Left empty (${empty.length}): ` + empty.map(nameOf).join(' · ')));
  }
  if (mine.learned) {
    results.append(el('div', 'learned', `Remembered ${mine.learned} new or updated ` +
      `fact${mine.learned > 1 ? 's' : ''} about you for the next form.`));
  }
  if (rows.length) {
    results.append(el('div', 'check', 'Nothing has been submitted. Check the page, then ' +
      'submit it yourself.'));
  }
  showMemory();
  $('memo').hidden = !session;
}

// -- wiring -----------------------------------------------------------------------

async function init() {
  let saved = {};
  try {
    saved = await chrome.storage.local.get(['server', 'note', 'dir', 'overwrite', 'memory']);
  } catch {
    // nothing remembered: the defaults do
  }
  server = saved.server || DEFAULT_SERVER;
  savedDir = saved.dir || '';
  $('server').value = server;
  $('note').value = saved.note || '';
  $('overwrite').checked = !!saved.overwrite;
  $('memory').checked = saved.memory !== false;
  windowId = (await chrome.windows.getCurrent()).id;

  $('fill').addEventListener('click', fillPage);
  $('stop').addEventListener('click', stop);
  $('rescan').addEventListener('click', () => scan());
  $('retry').addEventListener('click', async () => {
    server = $('server').value.trim().replace(/\/+$/, '') || DEFAULT_SERVER;
    remember({ server });
    if (await connect()) scan();
  });
  let noteTimer = 0;
  $('note').addEventListener('input', () => {
    clearTimeout(noteTimer);
    noteTimer = setTimeout(() => remember({ note: $('note').value }), 400);
  });
  $('dir').addEventListener('change', () => {
    remember({ dir: $('dir').value });
    session = '';  // the agent's earlier searches covered other folders
    $('memo').hidden = true;
  });
  $('overwrite').addEventListener('change', () => remember({ overwrite: $('overwrite').checked }));
  $('memory').addEventListener('change', () => {
    remember({ memory: $('memory').checked });
    session = '';  // the conversation was told about the memory, or wasn't
    $('memo').hidden = true;
  });
  $('fresh').addEventListener('click', event => {
    event.preventDefault();
    session = '';
    $('memo').hidden = true;
  });
  // The page is whichever tab is in front in this window.
  chrome.tabs.onActivated.addListener(info => info.windowId === windowId && rescanSoon());
  chrome.tabs.onUpdated.addListener((tabId, change, tab) => {
    if (tab.active && tab.windowId === windowId && change.status === 'complete') rescanSoon();
  });
  // Closing the panel stops its agent, rather than leave it working for nobody.
  addEventListener('pagehide', () => {
    if (run) fetch(server + '/api/fill/cancel', { method: 'POST', keepalive: true });
  });

  if (await connect()) await scan();
}

init();
