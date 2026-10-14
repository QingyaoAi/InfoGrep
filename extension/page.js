// What InfoGrep's side panel runs inside a page: it lists the form fields there, and puts
// values into them. The panel injects it on demand into each frame of the tab beside it,
// in the extension's isolated world, where the page's own scripts can neither see nor
// change it.
//
// Nothing here presses a button or submits a form. The only clicks are on a tick box or a
// radio button being set and, for a dropdown a page draws itself (Element, Ant Design, an
// ARIA combobox), on that dropdown's own box and on a choice in the list it opened.
(() => {
  if (globalThis.__infogrepFill) return;

  const CONTROLS = 'input, textarea, select';
  const OTHERS = 'input:not([type=hidden]), textarea, select';  // controls a person sees
  const EDITORS = '[contenteditable]:not([contenteditable="false"])';
  // Inputs with nothing to look up: buttons, files, and secrets no file should be asked for.
  const NOT_FIELDS = new Set(
    ['hidden', 'submit', 'button', 'reset', 'image', 'file', 'password', 'range', 'color']);
  const SECRET = /(^|\s)(cc-\S+|one-time-code|new-password|current-password)(\s|$)/;
  const UNREAD = 'input, textarea, select, option, button, script, style, noscript, template';
  const LABEL = 200;     // characters of a label, and of the text taken for one
  const OPTION = 80;
  const OPTIONS = 300;   // of one list
  const VALUE = 300;     // characters of what a field already holds

  // Dropdowns that pages draw, by the library that draws them: the box that opens one, the
  // popup it opens (often at the end of the page, away from the box), the choices there,
  // and for a cascader the columns of choices, one per level.
  const ANT = {
    root: '.ant-select:not(.ant-select-auto-complete)', trigger: '.ant-select-selector',
    popup: '.ant-select-dropdown', choice: '.ant-select-item-option',
    column: '.ant-cascader-menu', item: '.ant-cascader-menu-item', cascader: '.ant-cascader',
    disabled: '.ant-select-disabled', on: '.ant-select-item-option-selected',
    off: '.ant-select-item-option-disabled, .ant-cascader-menu-item-disabled',
  };
  const ELEMENT = {  // Element UI (Vue 2) and Element Plus (Vue 3)
    root: '.el-select, .el-cascader', trigger: '.el-select__wrapper, .el-input',
    popup: '.el-select__popper, .el-select-dropdown, .el-cascader__dropdown',
    choice: '.el-select-dropdown__item', column: '.el-cascader-menu', item: '.el-cascader-node',
    cascader: '.el-cascader', disabled: '.is-disabled', on: '.selected, .is-selected',
    off: '.is-disabled',
  };
  const ARIA = {  // any other dropdown that says what it is the way ARIA asks
    root: '[role="combobox"]:not(input), input[role="combobox"][readonly], ' +
      '[aria-haspopup="listbox"]:not(input)',
    trigger: '', popup: '[role="listbox"]', choice: '[role="option"]', column: '', item: '',
    cascader: '', disabled: '[aria-disabled="true"]', on: '[aria-selected="true"]',
    off: '[aria-disabled="true"]',
  };
  const FAMILIES = [ANT, ELEMENT, ARIA];
  const LIBRARIES = `${ANT.root}, ${ELEMENT.root}`;

  // A control keeps its number from one scan to the next, so a value worked out for a
  // field finds its way back to the element.
  const refs = [];
  const numbers = new WeakMap();
  function numberOf(el) {
    if (!numbers.has(el)) {
      numbers.set(el, refs.length);
      refs.push(new WeakRef(el));
    }
    return numbers.get(el);
  }
  const elementOf = n => refs[n]?.deref();

  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  // Ask `test` again and again until it gives something, for `most` ms at most.
  async function waitFor(test, most) {
    for (const end = Date.now() + most; ;) {
      const result = test();
      if (result || Date.now() > end) return result;
      await wait(50);
    }
  }

  // -- reading the page ---------------------------------------------------------

  const squash = text => String(text || '').replace(/\s+/g, ' ').trim();
  const clip = (text, limit) => (text.length > limit ? text.slice(0, limit - 1) + '…' : text);
  const bare = text => String(text || '').normalize('NFKC').replace(/\s+/g, '');

  // The first of several places a text may be, tried in turn.
  function first(...sources) {
    for (const source of sources) {
      const text = squash(typeof source === 'function' ? source() : source);
      if (text) return text;
    }
    return '';
  }

  function say(field, key, text, limit = LABEL) {
    if (squash(text)) field[key] = clip(squash(text), limit);
  }

  // A control's name or id, unless a library made it up (el-id-12-3, rc_select_0, :r5:).
  const nameOf = el => [el.name, el.id].find(name => name &&
    !/^(el-id-|rc_select_|:r|mui-|radix-|headlessui-|react-select-)|^[\d_-]+$/.test(name)) || '';

  const rendered = el => el.checkVisibility({ checkVisibilityCSS: true, visibilityProperty: true });

  // Where a person could reach it: displayed, not collapsed to nothing, and not parked
  // off the edge of the page, which is where forms keep the traps they set for bots.
  function shown(el) {
    if (!el || !rendered(el)) return false;
    const box = el.getBoundingClientRect();
    return box.width > 0 && box.height > 0 && box.right + scrollX > 0 && box.bottom + scrollY > 0;
  }

  // Components keep their inputs in shadow roots; a closed one still opens to an extension.
  function shadowOf(el) {
    if (el.shadowRoot || !el.localName.includes('-')) return el.shadowRoot;
    try {
      return chrome.dom.openOrClosedShadowRoot(el);
    } catch {
      return null;
    }
  }

  const isEditor = el => el.matches(EDITORS) && el.isContentEditable &&
    !el.parentElement?.isContentEditable;

  // Every control under a root, shadow roots included, in the order the page shows them:
  // inputs, lists, ARIA dropdowns and the boxes rich-text editors write in.
  function* controlsIn(root) {
    for (const el of root.querySelectorAll('*')) {
      if (el.matches(CONTROLS) || el.matches(ARIA.root) || isEditor(el)) yield el;
      const shadow = shadowOf(el);
      if (shadow) yield* controlsIn(shadow);
    }
  }

  function wanted(el) {
    if (el.matches(':disabled') || el.closest('[aria-hidden="true"], [inert]')) return false;
    if (el.localName === 'select') return shown(el);
    if (NOT_FIELDS.has(el.type) || SECRET.test(el.getAttribute('autocomplete') || '')) return false;
    // A styled tick box is often an invisible input under a drawing of one.
    if (el.type === 'radio' || el.type === 'checkbox') {
      return shown(el) || [...el.labels].some(shown) || shown(el.parentElement);
    }
    return !el.readOnly && shown(el);
  }

  // What a person reads in a node: its text, without what is inside a form control or
  // is not displayed.
  function textOf(node) {
    if (node.nodeType === Node.TEXT_NODE) return node.nodeValue;
    if (node.nodeType !== Node.ELEMENT_NODE || node.matches(UNREAD) || !rendered(node)) return '';
    let text = '';
    for (const child of node.childNodes) text += textOf(child) + ' ';
    return text;
  }

  // Far too much text to be a label, or another field's.
  const notLabel = node => node.nodeType === Node.ELEMENT_NODE &&
    (node.textContent.length > 2 * LABEL || (node.matches(OTHERS) && rendered(node)) ||
      [...node.querySelectorAll(OTHERS)].some(rendered));

  // The text just in front of a control that nothing is tied to as a label: the "Name:"
  // in the cell or on the line before it, `levels` parents up at most.
  function textBefore(el, levels = 4) {
    for (let node = el, up = 0; node && up < levels; node = node.parentElement, up++) {
      if (node.matches('form, fieldset, body')) break;
      for (let prev = node.previousSibling; prev; prev = prev.previousSibling) {
        if (prev.nodeType === Node.ELEMENT_NODE && !rendered(prev)) continue;  // read by no one
        if (notLabel(prev)) return '';
        const text = squash(textOf(prev));
        if (text) return text;
      }
    }
    return '';
  }

  // The text right after a tick box or radio button, which is where theirs goes.
  function textAfter(el) {
    for (let next = el.nextSibling; next; next = next.nextSibling) {
      if (notLabel(next)) return '';
      const text = squash(textOf(next));
      if (text) return text;
    }
    return '';
  }

  // The text of the elements an attribute names by id (aria-labelledby).
  function named(el, attribute) {
    const root = el.getRootNode();
    return (el.getAttribute(attribute) || '').split(/\s+/).filter(Boolean)
      .map(id => root.getElementById?.(id)).filter(Boolean).map(textOf).join(' ');
  }

  // What is tied to a control as its label.
  const labelOf = el => first(
    () => [...(el.labels || [])].map(textOf).join(' '),
    () => named(el, 'aria-labelledby'),
    el.getAttribute('aria-label'));

  const legendOf = el => {
    const legend = el.closest('fieldset')?.querySelector(':scope > legend');
    return legend ? squash(textOf(legend)) : '';
  };

  // Marked as required, by the browser's attribute or by the form library's own mark.
  const requiredHere = el => el.required || el.getAttribute('aria-required') === 'true' ||
    !!el.closest('.el-form-item.is-required') ||
    !!el.closest('.ant-form-item')?.querySelector('.ant-form-item-required');

  const optionText = el => clip(first(labelOf(el), () => textAfter(el), el.title, el.value), OPTION);

  // The radio buttons, or tick boxes, that share a control's name: one question's answers.
  function setOf(el) {
    if (!el.name) return [el];
    const home = el.form || el.getRootNode();
    const all = el.form ? [...el.form.elements] : [...home.querySelectorAll('input')];
    return all.filter(other => other.type === el.type && other.name === el.name &&
      (other.form || other.getRootNode()) === home);
  }

  // The question a set of radio buttons or tick boxes answers.
  function questionOf(set) {
    const el = set[0];
    const group = el.closest('[role="radiogroup"], [role="group"], fieldset');
    // A group's own name is the question only when the group holds nothing but the set.
    if (group && [...group.querySelectorAll(OTHERS)].every(other => set.includes(other))) {
      const name = first(() => named(group, 'aria-labelledby'), group.getAttribute('aria-label'),
        () => (group.matches('fieldset') ? legendOf(el) : ''));
      if (name) return name;
    }
    return textBefore(el.closest('label') || el);
  }

  // The headings of a root, in page order, for sectionOf.
  const headings = new Map();
  function headingsOf(root) {
    if (!headings.has(root)) {
      headings.set(root, [...root.querySelectorAll('h1, h2, h3, h4, h5, h6, [role="heading"]')]
        .filter(shown).map(el => ({ el, text: squash(textOf(el)) })).filter(h => h.text));
    }
    return headings.get(root);
  }

  // The part of the form a control is in: its fieldset's legend, else the heading above it.
  function sectionOf(el) {
    let section = legendOf(el);
    if (section) return section;
    for (const heading of headingsOf(el.getRootNode())) {
      if (!(heading.el.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING)) break;
      section = heading.text;
    }
    return section;
  }

  // A list's choices, less its "please choose" (an option with no value).
  const optionsOf = select => [...select.options]
    .filter(o => !o.disabled && o.value !== '' && squash(o.text))
    .slice(0, OPTIONS).map(o => clip(squash(o.text), OPTION));

  // One native field as the agent is told of it. A "value" is there only when someone has
  // already filled the field in: what it starts out as says nothing.
  function describe(el, sets) {
    const field = { n: numberOf(el), kind: el.localName === 'input' ? el.type : el.localName };
    say(field, 'name', nameOf(el));
    if (requiredHere(el)) field.required = true;

    if (el.type === 'radio') {
      const set = setOf(el);
      const option = optionText(el);
      const told = sets.get(set[0]);  // the set's field, once its first button has been met
      if (told) {
        told.options.push(option);
        if (el.checked && !el.defaultChecked) told.value = option;
        return null;
      }
      sets.set(set[0], field);
      say(field, 'label', questionOf(set));
      field.options = [option];
      if (el.checked && !el.defaultChecked) field.value = option;
      return field;
    }
    if (el.type === 'checkbox') {
      const set = setOf(el);  // one of several under a question is "question: this one"
      const question = set.length > 1 ? questionOf(set) : '';
      say(field, 'label', [question, optionText(el)].filter(Boolean).join(': '));
      if (el.checked !== el.defaultChecked) field.value = el.checked;
      return field;
    }

    say(field, 'label', first(labelOf(el), () => textBefore(el), el.title).slice(-LABEL));
    if (el.localName === 'select') {
      field.kind = el.multiple ? 'multiselect' : 'select';
      field.options = optionsOf(el);
      const chosen = [...el.selectedOptions].filter(o => o.value !== '').map(o => squash(o.text));
      if (el.multiple) {
        if ([...el.options].some(o => o.selected !== o.defaultSelected)) field.value = chosen;
      } else {
        const preset = [...el.options].findLast(o => o.defaultSelected) ||
          [...el.options].find(o => !o.disabled);
        if (chosen.length && el.selectedOptions[0] !== preset) field.value = chosen[0];
      }
      return field;
    }
    say(field, 'hint', first(el.placeholder, el.title === field.label ? '' : el.title));
    const autocomplete = squash(el.getAttribute('autocomplete')).toLowerCase();
    if (autocomplete && autocomplete !== 'on' && autocomplete !== 'off') {
      say(field, 'autocomplete', autocomplete);
    }
    if (el.maxLength > 0) field.maxlength = el.maxLength;
    say(field, 'value', el.value, VALUE);
    return field;
  }

  // -- dropdowns a page draws ---------------------------------------------------

  const popups = new WeakMap();   // dropdown -> the popup it opened last
  const offered = new WeakMap();  // dropdown -> the choices seen when one was looked for
  const hints = new WeakMap();    // cascader -> where its path stopped, and what was there

  const familyOf = root => FAMILIES.find(family => root.matches(family.root));
  const isCascader = root => !!familyOf(root).cascader && root.matches(familyOf(root).cascader);
  const triggerOf = root => (familyOf(root).trigger && root.querySelector(familyOf(root).trigger)) ||
    root;

  // The dropdown a control is part of, if it is part of one.
  function dropdownOf(el) {
    return el.closest(LIBRARIES) || (el.matches(ARIA.root) ? el : null);
  }

  // The popup a dropdown's combobox names as the list it controls.
  function linkedPopup(root) {
    const box = root.matches('[role="combobox"]') ? root : root.querySelector('[role="combobox"]');
    const id = (box?.getAttribute('aria-controls') || box?.getAttribute('aria-owns') || '')
      .split(/\s+/)[0];
    const list = id && root.getRootNode().getElementById?.(id);
    return list ? list.closest(familyOf(root).popup) || list : null;
  }

  // Its popup as far as it is known without opening it: Element UI keeps it inside the box
  // until it is first opened, Element Plus links to it, Ant Design draws it on first open.
  const knownPopup = root => popups.get(root) || linkedPopup(root) ||
    root.querySelector(familyOf(root).popup);

  const choicesIn = (popup, family, visible) => [...popup.querySelectorAll(family.choice)]
    .filter(choice => !choice.matches(family.off) && (!visible || rendered(choice)));
  const columnsIn = (popup, family, visible) => (family.column
    ? [...popup.querySelectorAll(family.column)].filter(column => !visible || rendered(column))
    : []);
  const itemsIn = (column, family) => [...column.querySelectorAll(family.item)]
    .filter(item => !item.matches(family.off));
  const choiceLabel = el => squash(el.getAttribute('title') || (el.querySelector(
    '.ant-select-item-option-content, .ant-cascader-menu-item-content, .el-cascader-node__label') ||
    el).textContent);
  const isOn = (choice, family) => choice.matches(family.on) ||
    choice.getAttribute('aria-selected') === 'true';

  function isMultiple(root) {
    const family = familyOf(root);
    if (family === ANT) return root.matches('.ant-select-multiple');
    const popup = knownPopup(root);
    if (family === ELEMENT) {
      return !!root.querySelector('.el-select__tags') || !!popup?.matches('.is-multiple') ||
        !!popup?.querySelector('.el-select-dropdown.is-multiple');
    }
    return popup?.getAttribute('aria-multiselectable') === 'true';
  }

  // What a dropdown shows as chosen, a label each ([] while it shows its placeholder).
  function chosenOf(root) {
    const family = familyOf(root);
    let texts;
    if (family === ANT) {
      texts = [...root.querySelectorAll('.ant-select-selection-item')]
        .filter(item => !item.closest('.ant-select-selection-overflow-item-rest'))
        .map(item => item.getAttribute('title') || item.textContent);
    } else if (family === ELEMENT) {
      const tags = root.querySelectorAll('.el-select__tags-text');
      const plus = root.querySelector('.el-select__placeholder');  // Element Plus
      texts = tags.length ? [...tags].map(tag => tag.textContent)
        : plus ? (plus.matches('.is-transparent') ? [] : [plus.textContent])
          : [root.querySelector('input')?.value];
    } else {
      texts = [root.localName === 'input' ? root.value : root.textContent];
    }
    return texts.map(text => squash(String(text || '').replace(/​/g, ''))).filter(Boolean);
  }

  function placeholderOf(root) {
    const text = root.querySelector(
      '.ant-select-selection-placeholder, .el-select__placeholder.is-transparent');
    return first(text?.textContent, root.querySelector('input')?.placeholder,
      root.getAttribute('aria-placeholder'));
  }

  // A dropdown as the agent is told of it: its choices as far as they are known without
  // opening it, and, once a choice was looked for, what it was seen to offer.
  function describeDropdown(root) {
    const family = familyOf(root);
    const input = root.querySelector('input');
    const cascader = isCascader(root);
    const kind = cascader ? 'cascader' : isMultiple(root) ? 'multiselect' : 'select';
    const field = { n: numberOf(root), kind };
    say(field, 'name', (input && nameOf(input)) || nameOf(root));
    say(field, 'label', first(input ? labelOf(input) : '', labelOf(root), () => textBefore(root)));
    if (requiredHere(input || root)) field.required = true;
    const popup = knownPopup(root);
    let options = [];
    if (popup && cascader) {
      const top = columnsIn(popup, family, false)[0];
      options = top ? itemsIn(top, family).map(choiceLabel) : [];
    } else if (popup && (family !== ANT || shown(popup))) {
      // Ant Design keeps a closed list as it last drew it: a filtered or scrolled part.
      options = choicesIn(popup, family, false).map(choiceLabel);
    }
    // A long list draws only some of its choices at a time: what a look through it saw
    // stands, until the list offers something it did not.
    const seen = offered.get(root);
    if (seen && options.every(option => seen.includes(option))) options = seen;
    else offered.delete(root);
    options = [...new Set(options.filter(Boolean))];
    if (options.length) field.options = options.slice(0, OPTIONS).map(o => clip(o, OPTION));
    say(field, 'hint', [placeholderOf(root), hints.get(root)].filter(Boolean).join(' · '), 2 * LABEL);
    const chosen = family === ARIA ? [] : chosenOf(root);  // an ARIA box may show its placeholder
    if (chosen.length) field.value = field.kind === 'multiselect' ? chosen : chosen.join(' / ');
    return field;
  }

  // -- rich-text editors --------------------------------------------------------

  function describeEditor(host) {
    const field = { n: numberOf(host), kind: 'richtext' };
    // An editor in a frame of its own (TinyMCE, CKEditor 4) is labelled out in the page.
    const frame = host === host.ownerDocument.body ? frameElement : null;
    say(field, 'label', first(() => named(host, 'aria-labelledby'), () => textBefore(frame || host, 8),
      host.getAttribute('aria-label'), frame?.title));
    say(field, 'hint', first(host.getAttribute('data-placeholder'),
      host.getAttribute('aria-placeholder'), host.getAttribute('placeholder')));
    if (requiredHere(frame || host)) field.required = true;
    say(field, 'value', host.innerText, VALUE);
    return field;
  }

  // -- the fields of a frame ----------------------------------------------------

  // The field a control makes, or null: a control not for filling in, or one met already
  // as part of a dropdown.
  function fieldOf(el, sets, met) {
    const dropdown = dropdownOf(el);
    if (dropdown) {
      if (met.has(dropdown)) return null;
      met.add(dropdown);
      const usable = shown(dropdown) && !dropdown.matches(familyOf(dropdown).disabled) &&
        !dropdown.closest('[aria-hidden="true"], [inert]');
      return usable ? describeDropdown(dropdown) : null;
    }
    if (isEditor(el)) {
      return shown(el) && !el.closest('[aria-hidden="true"]') ? describeEditor(el) : null;
    }
    return wanted(el) ? describe(el, sets) : null;
  }

  // The fields of this frame, in page order. A "section" is given where one begins.
  function scan() {
    headings.clear();
    const fields = [];
    const sets = new Map();
    const met = new Set();
    let section = '';
    for (const el of controlsIn(document)) {
      let field = null;
      try {
        field = fieldOf(el, sets, met);
      } catch {
        // a control too odd to read is left out
      }
      if (!field) continue;
      const here = sectionOf(elementOf(field.n));
      if (here && here !== section && here !== field.label) field.section = section = clip(here, LABEL);
      fields.push(field);
    }
    return fields;
  }

  // -- writing to the page ------------------------------------------------------

  // Tell the page the way typing or choosing would: frameworks keep their own copy of a
  // field and only take it from these events. (A value set from this isolated world goes
  // straight to the element, past the copy React tracks, so React sees that it changed.)
  function changed(el, typed) {
    const input = typed === undefined
      ? new Event('input', { bubbles: true, composed: true })
      : new InputEvent('input',
        { bubbles: true, composed: true, inputType: 'insertReplacementText', data: typed });
    el.dispatchEvent(input);
    el.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
  }

  // Mark a field for a moment, leaving nothing behind in the page.
  function flash(el) {
    const seen = shown(el) ? el : [...(el.labels || [])].find(shown) || el.parentElement || el;
    seen.animate([
      { outline: '2px solid #4f9cf9', outlineOffset: '2px' },
      { outline: '2px solid transparent', outlineOffset: '2px' },
    ], { duration: 1600, easing: 'ease-in' });
    return seen;
  }

  // Wait for the page to stop changing (the options a choice brings, a field it shows):
  // `quiet` ms without a change, after `least` ms and before `most` ms in all.
  function settle(quiet = 300, most = 3000, least = 0) {
    return new Promise(resolve => {
      const start = Date.now();
      let last = start;
      const observer = new MutationObserver(() => { last = Date.now(); });
      observer.observe(document, { subtree: true, childList: true, attributes: true, characterData: true });
      const check = () => {
        const now = Date.now();
        if (now - start >= most || (now - start >= least && now - last >= quiet)) {
          observer.disconnect();
          resolve(true);
        } else {
          setTimeout(check, 50);
        }
      };
      setTimeout(check, 50);
    });
  }

  // A date the way a date field takes it, from the ways one gets written (2020/1/5, 2020年1月5日).
  function shaped(el, text) {
    const date = text.match(/(\d{4})\D+(\d{1,2})(?:\D+(\d{1,2}))?/);
    const two = part => part.padStart(2, '0');
    if (el.type === 'date' && date?.[3]) return `${date[1]}-${two(date[2])}-${two(date[3])}`;
    if (el.type === 'month' && date) return `${date[1]}-${two(date[2])}`;
    return text;
  }

  // The choice that answers `want`: one with the same text, else the only one whose text
  // begins with it or that it begins with ("海淀" and "海淀区").
  function pick(choices, want, label) {
    const asked = squash(want).toLowerCase();
    if (!asked) return null;
    const texts = choices.map(choice => squash(label(choice)).toLowerCase());
    const same = texts.indexOf(asked);
    if (same >= 0) return choices[same];
    const near = choices.filter((_, i) => Math.min(texts[i].length, asked.length) >= 2 &&
      (texts[i].startsWith(asked) || asked.startsWith(texts[i])));
    return near.length === 1 ? near[0] : null;
  }

  function choose(el, value) {
    const options = [...el.options].filter(o => !o.disabled);
    const find = want => options.find(o => o.value !== '' && o.value === want) ||
      pick(options.filter(o => o.value !== ''), want, o => o.text);
    const picked = [].concat(value).map(want => find(String(want)));
    if (!picked.length || picked.includes(null) || picked.includes(undefined) ||
      (picked.length > 1 && !el.multiple)) {
      return 'option';
    }
    if (el.multiple) for (const option of options) option.selected = picked.includes(option);
    else picked[0].selected = true;
    changed(el);
    return '';
  }

  // Never a link, a button or anything else that goes somewhere or sends something.
  function harmless(target) {
    for (let el = target; el; el = el.parentElement) {
      if (el.matches('a[href], input[type=submit], input[type=image], input[type=reset], ' +
        'input[type=button], [type=submit]')) return false;
      if (el.localName === 'button' && el.type !== 'button') return false;  // a plain one submits
    }
    return true;
  }

  // A click as a mouse makes one. Only ever on a dropdown's own box or on a choice in the
  // list it opened (`within`), and only on what is harmless.
  function press(target, within) {
    if (!within.some(scope => scope?.contains(target)) || !harmless(target)) {
      throw new Error('not a click this is allowed to make');
    }
    const box = target.getBoundingClientRect();
    const at = { bubbles: true, cancelable: true, composed: true, view: window, button: 0,
      clientX: box.left + box.width / 2, clientY: box.top + box.height / 2 };
    const pointer = { ...at, pointerId: 1, pointerType: 'mouse', isPrimary: true };
    target.dispatchEvent(new PointerEvent('pointerdown', { ...pointer, buttons: 1 }));
    target.dispatchEvent(new MouseEvent('mousedown', { ...at, buttons: 1 }));
    target.dispatchEvent(new PointerEvent('pointerup', pointer));
    target.dispatchEvent(new MouseEvent('mouseup', at));
    target.dispatchEvent(new MouseEvent('click', at));
  }

  function typeInto(input, text) {
    input.focus();
    input.value = text;
    input.dispatchEvent(new InputEvent('input',
      { bubbles: true, composed: true, inputType: 'insertText', data: text }));
  }

  const shownPopups = (root, family) => [...root.ownerDocument.querySelectorAll(family.popup)]
    .filter(shown);

  // Open a dropdown; returns the popup it opened, or null.
  async function open(root) {
    const family = familyOf(root);
    const before = new Set(shownPopups(root, family));
    const opened = () => {
      const linked = linkedPopup(root);
      if (linked && shown(linked)) return linked;
      return shownPopups(root, family).find(popup => !before.has(popup)) || null;
    };
    // Some boxes open on the press and close again on the click: then the second time.
    for (let attempt = 0; attempt < 2; attempt++) {
      press(triggerOf(root), [root]);
      const popup = await waitFor(opened, 1200);
      if (popup) {
        popups.set(root, popup);
        return popup;
      }
    }
    return null;
  }

  async function close(root, popup) {
    const gone = () => !shown(popup);
    const input = root.querySelector('input') || root;
    input.dispatchEvent(new KeyboardEvent('keydown',
      { key: 'Escape', code: 'Escape', bubbles: true, cancelable: true, composed: true }));
    if (await waitFor(gone, 300)) return;
    press(triggerOf(root), [root]);
    if (await waitFor(gone, 300)) return;
    // A press somewhere else, which is how a dropdown expects to be dismissed. There is
    // no click in it: nothing on the page is hit.
    const away = { bubbles: true, cancelable: true, composed: true, view: window, button: 0 };
    document.documentElement.dispatchEvent(new MouseEvent('mousedown', away));
    document.documentElement.dispatchEvent(new MouseEvent('mouseup', away));
    await waitFor(gone, 300);
  }

  // Scroll a long list through, for lists that only draw the choices in view.
  async function scrollFor(popup, look) {
    const holder = [popup, ...popup.querySelectorAll('*')]
      .find(el => el.clientHeight > 0 && el.scrollHeight > el.clientHeight + 8);
    if (!holder) return null;
    for (let step = 0, stuck = 0; step < 80 && stuck < 2; step++) {
      const top = holder.scrollTop;
      holder.scrollTop = top + holder.clientHeight * 0.8;
      if (holder.scrollTop === top) {  // a list that scrolls by the wheel alone
        holder.dispatchEvent(new WheelEvent('wheel',
          { deltaY: holder.clientHeight * 0.8, bubbles: true, cancelable: true }));
      }
      await wait(80);
      const found = look();
      if (found) return found;
      stuck = holder.scrollTop === top ? stuck + 1 : 0;
    }
    return null;
  }

  // Find the choice for `want` in an open dropdown: in view, through its search box, or
  // further down a long list. Remembers what it saw, for when it is not there.
  async function findChoice(root, popup, want) {
    const family = familyOf(root);
    const seen = new Set();
    const look = () => {
      const choices = choicesIn(popup, family, true);
      for (const choice of choices) seen.add(choiceLabel(choice));
      return pick(choices, want, choiceLabel);
    };
    let found = await waitFor(look, 600);  // the list may still be on its way
    const search = family === ARIA ? null : root.querySelector('input:not([readonly])');
    if (!found && search) {
      typeInto(search, String(want));
      found = await waitFor(look, 1500);
      if (!found) typeInto(search, '');
    }
    if (!found) found = await scrollFor(popup, look);
    if (!found) offered.set(root, [...seen].filter(Boolean).slice(0, OPTIONS));
    return found;
  }

  // Walk a cascader's levels along `path`, which need not match them one for one: a level
  // with a single choice that the path leaves out ("市辖区" between "北京市" and "海淀区")
  // is taken on the way, and a step of the path that no level has ("北京市 / 北京市 /
  // 海淀区") is passed over once a later step is found.
  async function walk(root, popup, path, took) {
    const family = familyOf(root);
    let i = 0;
    for (let level = 0; level < 8 && i < path.length && shown(popup); level++) {
      // The next level shows once the one before has a choice; a site may fetch it first.
      const column = await waitFor(() => columnsIn(popup, family, true)[level], 1500);
      if (!column) break;
      const items = itemsIn(column, family);
      let item = null;
      for (let j = i; j < path.length && !item; j++) {
        item = pick(items, path[j], choiceLabel);
        if (item) i = j + 1;
      }
      if (!item && level > 0 && items.length === 1) item = items[0];
      if (!item) {
        const where = took.length ? `after ${took.join(' / ')}, ` : '';
        hints.set(root, clip(`${where}the choices are: ${items.map(choiceLabel).join(', ')}`, 2 * LABEL));
        return 'option';
      }
      took.push(choiceLabel(item));
      press(item, [popup]);
      await wait(150);
    }
    // Still open with a level after the last choice: the path stopped short of the end.
    const next = shown(popup) && columnsIn(popup, family, true)[took.length];
    if (next) {
      hints.set(root, clip(`after ${took.join(' / ')}, the choices are: ` +
        itemsIn(next, family).map(choiceLabel).join(', '), 2 * LABEL));
      return 'option';
    }
    hints.delete(root);
    return took.length ? '' : 'option';
  }

  async function putDropdown(root, value) {
    const family = familyOf(root);
    if (root.matches(family.disabled)) return 'locked';
    const popup = await open(root);
    if (!popup) return 'refused';
    const took = [];
    let error = '';
    try {
      if (isCascader(root)) {
        error = await walk(root, popup, [].concat(value), took);
      } else {
        for (const want of [].concat(value)) {
          const choice = await findChoice(root, popup, want);
          if (!choice) {
            error = 'option';
            break;
          }
          took.push(choiceLabel(choice));
          if (!isOn(choice, family)) press(choice, [popup]);
          await wait(100);
        }
      }
    } finally {
      if (!(await waitFor(() => !shown(popup), 400))) await close(root, popup);
    }
    if (error) return error;
    // It took when the box shows what was chosen.
    const shows = () => {
      const now = chosenOf(root).join(' / ').toLowerCase();
      return took.every(label => now.includes(label.toLowerCase()));
    };
    return (await waitFor(shows, 800)) ? '' : 'refused';
  }

  // The text an editor drew from its own model, where it marks that text (Slate, Lexical,
  // Draft.js); elsewhere all of the box. Typing that went past such an editor's model is
  // in the box but not here, so it does not pass for a value that is in.
  function modelText(host) {
    const drawn = host.querySelectorAll('[data-slate-string], [data-lexical-text], [data-text="true"]');
    return drawn.length ? [...drawn].map(node => node.textContent).join('\n') : host.innerText;
  }

  // Put prose in an editor the way a paste does, step by step as the browser makes one:
  // the paste, then the input it turns into, then the text itself, each only when nothing
  // took the one before. An editor takes the first it handles into its own model.
  async function putProse(host, text) {
    const doc = host.ownerDocument;
    const took = () => {
      const now = bare(modelText(host));
      return now.includes(bare(text.slice(0, 40))) && now.includes(bare(text.slice(-40)));
    };
    host.focus();
    const range = doc.createRange();
    range.selectNodeContents(host);
    const selection = doc.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    await wait(200);  // editors hear of the selection a moment later
    const data = new DataTransfer();
    data.setData('text/plain', text);
    const paste = new ClipboardEvent('paste',
      { clipboardData: data, bubbles: true, cancelable: true, composed: true });
    host.dispatchEvent(paste);
    if (!paste.defaultPrevented) {
      const input = new InputEvent('beforeinput', { inputType: 'insertFromPaste',
        dataTransfer: data, bubbles: true, cancelable: true, composed: true });
      host.dispatchEvent(input);
      if (!input.defaultPrevented) {
        text.split('\n').forEach((line, i) => {
          if (i) doc.execCommand('insertParagraph');
          if (line) doc.execCommand('insertText', false, line);
        });
      }
    }
    return (await waitFor(took, 1000)) ? '' : 'refused';
  }

  // Put a value in a field. Returns '' when it is in, else why not: 'locked' (the field
  // takes no input), 'option' (not one the field offers) or 'refused' (not in its format).
  async function put(el, value) {
    if (dropdownOf(el) === el) return putDropdown(el, value);
    if (isEditor(el)) return putProse(el, String(value));
    if (el.matches(':disabled') || el.readOnly) return 'locked';
    if (el.localName === 'select') return choose(el, value);
    // Ticking is a click on the box itself, which is what tells the page; never on a button.
    if (el.type === 'radio') {
      const want = String(value);
      const set = setOf(el);
      const radio = set.find(r => r.value === want) || pick(set, want, optionText);
      if (!radio) return 'option';
      if (!radio.checked) radio.click();
      return radio.checked ? '' : 'refused';
    }
    if (el.type === 'checkbox') {
      if (el.checked !== (value === true)) el.click();
      return el.checked === (value === true) ? '' : 'refused';
    }
    const text = shaped(el, String(value));
    el.value = text;
    if (!el.value) return 'refused';  // a number or date field drops what it can't read
    changed(el, text);
    return '';
  }

  const choosing = el => el.localName === 'select' || el.type === 'radio' ||
    el.type === 'checkbox' || dropdownOf(el) === el;

  // What a list offers now, as a scan reports it, for a value it did not have: a list that
  // fills in later (the cities of a province) is tried again once it offers more.
  function offers(el) {
    if (el.localName === 'select') return optionsOf(el);
    if (el.type === 'radio') return setOf(el).map(optionText);
    return dropdownOf(el) === el ? describeDropdown(el).options || [] : [];
  }

  // items: [{n, value}], in page order. Returns [{n, error}], error '' for a value that is
  // in, and for a value a list did not offer, the choices it had then.
  async function fill(items) {
    const results = [];
    for (const { n, value } of items) {
      const el = elementOf(n);
      const result = { n, error: 'gone' };
      if (el?.isConnected) {
        try {
          result.error = await put(el, value);
        } catch {
          result.error = 'refused';
        }
        if (!result.error) {
          flash((el.type === 'radio' && setOf(el).find(radio => radio.checked)) || el);
          // A choice can change the form: let what it brings arrive before the next field.
          if (choosing(el)) await settle(200, 1500);
        } else if (result.error === 'option') {
          result.options = offers(el);
        }
      }
      results.push(result);
    }
    return results;
  }

  // Bring a field into view and mark it.
  function show(n) {
    const el = elementOf(n);
    if (!el?.isConnected) return false;
    flash(el).scrollIntoView({ block: 'center', behavior: 'smooth' });
    return true;
  }

  globalThis.__infogrepFill = { scan, fill, show, settle };
})();
