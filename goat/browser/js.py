"""All page-side JavaScript in one place. These strings are evaluated with Playwright's
page.evaluate; each is a function expression taking its arguments explicitly.

JS_PAGE_ERRORS and JS_SUBMIT_SIGNAL are ported from the Hermes pipeline, where they ran live:
the error scan caught "Please enter your home address" on Macy's, and the submit signal
confirmed the submission 65 seconds later. Regex vetoes for Oracle's role=alert status chatter
and Greenhouse helper text are the two false positives found in production."""

# Helpers shared by several scripts (injected by string concatenation).
_COMMON = r"""
  const vis = (el) => {
    try {
      const r = el.getBoundingClientRect();
      const s = el.ownerDocument.defaultView.getComputedStyle(el);
      return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' &&
             parseFloat(s.opacity || '1') > 0.01;
    } catch (e) { return false; }
  };
  // icon-font glyphs (private use area) are not text: an empty SuccessFactors dropdown shows only its arrow glyph U+E1EF
  const txt = (el) => ((el && (el.innerText || el.textContent)) || '').replace(/[\u200B-\u200D\u2060\uFEFF\uE000-\uF8FF]/g, '').replace(/\s+/g, ' ').trim();
  // Text that includes what <slot>s show: a design-system label keeps its words in the host's light DOM and its shadow
  // <label> holds only a slot and the '*' (SmartRecruiters' screening questions read as just '*', CapTech 2026-10-21).
  const ctxt = (el) => {
    if (!el || !el.querySelector || !el.querySelector('slot')) return txt(el);
    const parts = [];
    const walk = (n, d) => {
      if (!n || d > 40) return;
      if (n.nodeType === 3) { parts.push(n.nodeValue || ''); return; }
      if (n.nodeType !== 1 || n.tagName === 'STYLE' || n.tagName === 'SCRIPT') return;
      if (n.tagName === 'SLOT') {
        const as = n.assignedNodes ? n.assignedNodes({ flatten: true }) : [];
        for (const a of (as.length ? as : Array.from(n.childNodes))) walk(a, d + 1);
        return;
      }
      for (const c of n.childNodes) walk(c, d + 1);
    };
    walk(el, 0);
    return txt({ innerText: parts.join(' ') });
  };
  const wordy = (t) => !!(t && t.replace(/[\s*:]+/g, ''));
  const PLACEHOLDER = /^(select one|select\.\.\.|select…|select|choose one|choose|search|please select|-- select --|--|)$/i;
  // A field whose own label ends in "*" is required even without a required / aria-required attribute (Epic Games'
  // "Email*:", "School*:", "Gender Identity*:", 2026-10-19: the gate passed with all of them blank). The label is the
  // first plain label / legend above the control inside its own field box; a box holding other fields ends the search.
  const STAR_RE = /\*\s*:?\s*$/;
  const zw = (s) => (s || '').replace(/[\u200B-\u200D\u2060\uFEFF]/g, '').replace(/\s+/g, ' ').trim();
  // The first plain label / legend above a control inside its own field box (a box holding other fields ends the
  // search): Epic Games keeps "<label>Email*:</label>" in a sibling block whose for= names no element.
  function boxHead(el) {
    const choice = el.type === 'radio' || el.type === 'checkbox';
    let p = el.parentElement;
    for (let i = 0; i < 12 && p && p !== document.body; i++, p = p.parentElement) {
      const others = Array.from(p.querySelectorAll('input:not([type="hidden"]), select, textarea'))
        .filter(f => f !== el && !(choice && f.name && f.name === el.name));
      if (others.some(f => { const r = f.getBoundingClientRect(); return r.width > 0 && r.height > 0; })) return null;
      // an option's own label (for= a radio / tick box) is never the question: Applied Materials' and Qualcomm's Eightfold
      // radio groups read as their first option ('Yes', 'Male', "I'm in my second to last year of school", 2026-10-21)
      const heads = Array.from(p.querySelectorAll('label, legend')).filter(l => !l.contains(el) && !l.querySelector('input, select, textarea')
        && !(choice && l.tagName === 'LABEL' && l.control && /^(radio|checkbox)$/.test(l.control.type || '')));
      if (heads.length) return heads[0];
    }
    return null;
  }
  function starRequired(el) {
    const choice = el.type === 'radio' || el.type === 'checkbox';
    const own = el.labels ? Array.from(el.labels) : [];
    if (!choice && own.length) return own.some(l => STAR_RE.test(zw(l.innerText || l.textContent)));
    const h = boxHead(el);
    return !!(h && STAR_RE.test(zw(h.innerText || h.textContent)));
  }

  function labelFor(el) {
    const doc = el.ownerDocument; let t = '';
    const rn = el.getRootNode ? el.getRootNode() : doc;
    const scope = (rn && rn !== doc && rn.querySelector) ? rn : doc;
    if (el.id) { try { const l = scope.querySelector('label[for="' + CSS.escape(el.id) + '"]') || (scope !== doc ? doc.querySelector('label[for="' + CSS.escape(el.id) + '"]') : null); if (l) t = ctxt(l); } catch (e) {} }
    if (!wordy(t)) { const l = el.closest('label'); if (l) t = ctxt(l); }
    if (!t) t = el.getAttribute('aria-label') || '';
    let selectedList = false;
    if (/^items selected$/i.test(t) || (el.getAttribute && el.getAttribute('data-automation-id') === 'selectedItemList')) { t = ''; selectedList = true; }
    if (!wordy(t)) { const ids = el.getAttribute('aria-labelledby'); if (ids) { const t2 = ids.split(/\s+/).map(i => { const n = (scope.getElementById ? scope.getElementById(i) : null) || doc.getElementById(i); return n ? ctxt(n) : ''; }).filter(Boolean).join(' '); if (t2) t = t2; } }
    // a design-system control host names itself: SmartRecruiters' <spl-radio label="Yes"> draws its text in its own shadow root
    if (!wordy(t) && el.getAttribute && (el.getAttribute('label') || '').trim()) t = el.getAttribute('label').trim();
    if (!wordy(t) && el.shadowRoot) { const l = el.shadowRoot.querySelector('label'); if (l && wordy(ctxt(l))) t = ctxt(l); }
    if (!t) { const ff = el.closest('[data-automation-id^="formField"], .field, .form-group, .application-field, [class*="field" i], fieldset'); if (ff) { const l = ff.querySelector('label, legend, [class*="label" i]'); if (l && !l.contains(el)) t = txt(l); } }
    // Lever custom questions: the text sits in a sibling block BEFORE the field container (.application-label + .application-field)
    if (!t) { const ff = el.closest('.application-field, [class*="field" i]'); let p = ff && ff.previousElementSibling; for (let i = 0; i < 2 && p && !t; i++, p = p.previousElementSibling) { if (/label|question/i.test(p.className || '')) t = txt(p).replace(/\s*[✱*]\s*$/, ''); } }
    if (!t) { const h = boxHead(el); if (h) t = txt(h); }
    // inside a custom element's shadow root: the host's name ('I currently work here', 'Add experience entry')
    if (!t) { const hn = shadowHostNames(el); if (hn.length) t = hn[0]; }
    // no label anywhere: the question is the block just before the field's box (Rippling's custom questions read as
    // their name 'YIhsc0xVmXU', 2026-10-21)
    if (!wordy(t)) {
      let box = el.closest('[data-testid="field"]') || el.parentElement;
      for (let i = 0; i < 3 && box && !wordy(t); i++, box = box.parentElement) {
        const prev = box.previousElementSibling;
        if (prev && !prev.querySelector('input, select, textarea, button')) { const pt = txt(prev); if (pt && pt.length < 400) t = pt; }
      }
    }
    if (!t) t = el.getAttribute('placeholder') || el.getAttribute('name') || el.id || '';
    if (selectedList) {
      // Workday keeps the search box inside the chip list once something is selected: say so, and name the section.
      if (!t) { const sec = el.closest('[data-automation-id="multiSelectContainer"]'); const lab = sec && sec.parentElement && sec.parentElement.querySelector('label, [data-automation-id="formLabel"]'); if (lab) t = txt(lab); }
      t = (t || 'selected items') + ' (selected items; choose on this control adds another)';
    }
    t = t.replace(/\s+/g, ' ').trim();
    // a long label that explains first and asks last keeps its question when it is cut: Qualcomm's "Answering 'No' to the
    // following questions will not bar you ... approval to work in the U.S. Are you currently legally authorized to work
    // in the US?" was cut before the question, read as 'need assistance' and answered No (2026-10-21)
    if (t.length > 160) {
      const qs = t.match(/[^.?!]*\?/g);
      const q = qs && qs.length ? qs[qs.length - 1].trim() : '';
      if (q.length > 8 && !t.startsWith(q)) t = q + ' (' + t.replace(q, '').trim() + ')';
    }
    return t.slice(0, 200);
  }
  function* allRoots(root) {
    yield root;
    const els = root.querySelectorAll ? root.querySelectorAll('*') : [];
    for (const el of els) {
      if (el.shadowRoot) yield* allRoots(el.shadowRoot);
      if (el.tagName === 'IFRAME') { try { if (el.contentDocument) yield* allRoots(el.contentDocument); } catch (e) {} }
    }
  }
  // A control drawn inside a custom element's shadow root (SmartRecruiters' spl-button / spl-checkbox, 2026-10-21) has
  // no text or label of its own: its host carries the name (aria-label 'Add experience entry') and the slotted text ('Add').
  // A UI5 widget nests its input two shadow roots deep and names the outer host with accessible-name / title
  // (SuccessFactors' 'When are you available to start work?' date picker, Westinghouse 2026-10-23: the input had no label,
  // the run left it empty, and the judge failed the draft): the nearest host up to three levels that names the control.
  function shadowHostNames(el) {
    const out = [];
    let n = el;
    for (let i = 0; i < 3; i++) {
      const r = n && n.getRootNode ? n.getRootNode() : null;
      const h = r && r.host ? r.host : null;
      if (!h) break;
      for (const attr of ['aria-label', 'accessible-name', 'title']) {
        const a = (h.getAttribute(attr) || '').replace(/\s+/g, ' ').trim();
        if (a && !out.includes(a.slice(0, 120))) out.push(a.slice(0, 120));
      }
      const t = txt(h).slice(0, 120);
      if (t && !out.includes(t)) out.push(t);
      if (out.length) break;
      n = h;
    }
    return out;
  }
  // The pick a design-system field keeps on its custom-element host while its own box is empty (SmartRecruiters' City
  // after a page change: the box '', the host holding {displayString: 'Sugar Land, TX, US'}, ServiceNow 2026-10-21).
  function hostPickedValue(el) {
    let n = el;
    for (let i = 0; i < 3; i++) {
      const r = n && n.getRootNode ? n.getRootNode() : null;
      const h = r && r.host ? r.host : null;
      if (!h) return '';
      let v = h.value;
      // a design-system select keeps option ids (SmartRecruiters' spl-autocomplete / spl-multiselect-autocomplete): its
      // options' own text is the pick; an id with no option to name it is no value (LinkedIn, 2026-10-21)
      const ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
      if (Array.isArray(v) || (typeof v === 'string' && ID_RE.test(v.trim()))) {
        const ids = (Array.isArray(v) ? v : [v]).map(x => String(x).trim());
        const names = h.shadowRoot ? Array.from(h.shadowRoot.querySelectorAll('spl-select-option, [role="option"]'))
          .filter(o => ids.includes(String(o.getAttribute('value') || '')))
          .map(o => (o.textContent || '').replace(/\s+/g, ' ').trim()).filter(Boolean) : [];
        if (names.length) return names.join('; ');
        // a server-fed list drops its options once closed; the picks stay as chips (spl-tag label) in its tags list
        const tl = (el.closest && el.closest('spl-tags-list')) || (h.shadowRoot && h.shadowRoot.querySelector('spl-tags-list'));
        const chips = tl && tl.shadowRoot ? Array.from(tl.shadowRoot.querySelectorAll('spl-tag'))
          .map(t => (t.getAttribute('label') || t.textContent || '').replace(/\s+/g, ' ').trim()).filter(Boolean) : [];
        if (chips.length) return chips.join('; ');
        n = h; continue;
      }
      if (v && typeof v === 'object') v = v.displayString || v.text || v.name || v.label || '';
      if (typeof v === 'string' && v.trim()) return v.trim();
      n = h;
    }
    return '';
  }
  function findRef(ref) {
    for (const root of allRoots(document)) {
      try { const el = root.querySelector('[data-goat-ref="' + ref + '"]'); if (el) return el; } catch (e) {}
    }
    return null;
  }
"""

def _COMMON_WRAP(body: str, arg: str = "args") -> str:
    """`(arg) => { <_COMMON helpers> <body> }`."""
    return "(" + arg + ") => {\n" + _COMMON + "\n" + body + "\n}"


# Which of these refs are still on the page (in any shadow root).
JS_REFS_PRESENT = _COMMON_WRAP("""
  const out = [];
  for (const r of (args || [])) { try { if (findRef(r)) out.push(r); } catch (e) {} }
  return out;
""", "args")


# --------------------------------------------------------------------------- snapshot
JS_SNAPSHOT = r"""
(maxItems) => {
""" + _COMMON + r"""
  let counter = 0;
  const used = new Set();
  for (const root of allRoots(document)) for (const el of root.querySelectorAll('[data-goat-ref]')) {
    const n = parseInt((el.getAttribute('data-goat-ref') || 'e0').slice(1), 10); if (n > counter) counter = n; used.add(el);
  }
  const ref = (el) => { let r = el.getAttribute('data-goat-ref'); if (!r) { r = 'e' + (++counter); el.setAttribute('data-goat-ref', r); } return r; };
  const bbox = (el) => { const r = el.getBoundingClientRect(); return [Math.round(r.x + r.width / 2), Math.round(r.y + r.height / 2), Math.round(r.width), Math.round(r.height)]; };
  const inView = (el) => { const r = el.getBoundingClientRect(); return r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth; };
  const items = [];
  const groups = new Map();
  const seen = new Set();
  const SEL = 'input, textarea, select, button, a[href], [role="button"], [role="combobox"], [role="listbox"], [role="checkbox"], [role="radio"], [role="switch"], [role="tab"], [role="menuitem"], [role="option"], [contenteditable="true"], [data-automation-id="selectWidget"], [data-automation-id="promptOption"], [aria-haspopup], .beautiful-timeline-item[data-profile-item-tile]';

  function groupLabel(el) {
    // A group that names its question by aria-labelledby says so first (Eightfold's radio groups, 2026-10-21).
    const rg0 = el.closest('[role="radiogroup"], [role="group"], fieldset');
    const lb0 = rg0 ? (rg0.getAttribute('aria-labelledby') || '') : '';
    if (lb0) {
      const rr = rg0.getRootNode ? rg0.getRootNode() : document; const id0 = lb0.split(/\s+/)[0];
      const n0 = (rr && rr.getElementById ? rr.getElementById(id0) : null) || document.getElementById(id0);
      if (n0) { const t = ctxt(n0); if (wordy(t) && t.length < 300) return t; }
    }
    // The group's own box first: the nearest container holding only this group and a question label (Epic Games'
    // "Veteran Status*:" sits beside its options; a container classed "...Questions" held every demographic question,
    // so its first label named another question and "Hispanic or Latino", an option, read as a question, 2026-10-19).
    const own = boxHead(el); if (own) { const t0 = ctxt(own); if (wordy(t0) && t0.length < 300) return t0; }
    const fs = el.closest('fieldset, [role="group"], [role="radiogroup"], [data-automation-id^="formField"], .field, .form-group, .application-question, [class*="question" i]');
    if (fs) {
      // never one of the group's own option labels
      const opts = new Set(Array.from(fs.querySelectorAll('input[type="radio"], input[type="checkbox"]')).map(i => {
        const l = i.labels && i.labels[0]; return l ? txt(l) : ''; }).filter(Boolean));
      const lg = Array.from(fs.querySelectorAll('legend, [class*="legend" i], [id$="-label"], [id$="_label"], [class*="question" i] > :first-child, label:not([for]), .label, [class*="label" i]'))
        .find(x => !x.contains(el) && !x.querySelector('input[type="radio"], input[type="checkbox"]') && !opts.has(txt(x))
          && !(x.tagName === 'LABEL' && x.control && /^(radio|checkbox)$/.test(x.control.type || '')));
      if (lg) { const t = txt(lg); if (t && t.length < 300) return t; }
      const lb = fs.getAttribute('aria-labelledby'); if (lb) { const n = document.getElementById(lb.split(' ')[0]); if (n) return txt(n).slice(0, 300); }
    }
    // a question label in a sibling block above the options (Epic Games' "Gender Identity*:", 2026-10-19)
    const h = boxHead(el); if (h) { const t = ctxt(h); if (wordy(t) && t.length < 300) return t; }
    return '';
  }

  for (const root of allRoots(document)) {
    let els = []; try { els = root.querySelectorAll(SEL); } catch (e) { continue; }
    for (const el of els) {
      if (seen.has(el)) continue; seen.add(el);
      const tag = el.tagName.toLowerCase();
      const type = (el.getAttribute('type') || '').toLowerCase();
      if (type === 'hidden') continue;
      // Ashby's Yes/No question mirrors its two toggle buttons in an untabbable checkbox: the buttons are the control.
      // Setting the mirror too knocked Niantic's answer out of sync and the submit was refused (2026-10-18).
      if (tag === 'input' && (type === 'checkbox' || type === 'radio') && el.getAttribute('tabindex') === '-1'
          && el.parentElement && el.parentElement.querySelector('button[aria-pressed], [role="button"][aria-pressed]')) continue;
      // An aria-hidden, untabbable text box is a layout mirror, not a field: Oracle sizes each textarea with a hidden
      // copy of its text, and the gate demanded a decision for every copy (onsemi, 2026-10-19).
      if ((tag === 'textarea' || (tag === 'input' && /^(text|)$/.test(type))) && el.getAttribute('aria-hidden') === 'true'
          && el.getAttribute('tabindex') === '-1') continue;
      // Site chrome (header/nav menus, account and language switchers) is not part of the form.
      // A footer or header INSIDE a form, dialog or main area is the form's own (Oracle's apply flow keeps NEXT / CANCEL in
      // <footer class="apply-flow-pagination__buttons">; dropped as site chrome, AutoZone and American Express had no
      // NEXT to click, 2026-10-19).
      const chromeEl = el.closest('header, nav, [role="banner"], [role="navigation"], [data-automation-id="utilityButtonBar"], [data-automation-id="header"], footer, [role="contentinfo"]');
      const formOwned = !!(chromeEl && chromeEl.closest('form, dialog, [role="dialog"], main, [class*="apply-flow" i]'));
      if (chromeEl && !formOwned) {
        if (!(tag === 'input' || tag === 'textarea' || tag === 'select')) continue;
      }
      const isFile = type === 'file';
      // react-select style inputs are rendered with opacity 0 and width 1px inside a visible control;
      // treat them as visible when their control container is, and position them on the container.
      const comboLike = tag === 'input' && (el.getAttribute('aria-autocomplete') || el.getAttribute('aria-haspopup') || /react-select|select__input|__input/i.test(el.className || ''));
      const choiceLike = tag === 'input' && (type === 'checkbox' || type === 'radio');
      let posEl = el;
      // An action shown only on hover or focus is still a control: Oracle's tile pencil ('Edit') sits at opacity 0 beside
      // its always-shown Delete, and the run clicked Delete to edit a tile and lost an entry (AutoZone, 2026-10-21).
      const hoverOnly = (tag === 'button' || el.getAttribute('role') === 'button') && !vis(el)
        && !!el.closest('[class*="tile" i], [class*="card" i]') && (() => {
          try {
            const r = el.getBoundingClientRect(); const s = el.ownerDocument.defaultView.getComputedStyle(el); const p = el.parentElement;
            return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && !!p
              && Array.from(p.children).some(c => c !== el && vis(c) && (c.tagName === 'BUTTON' || c.getAttribute('role') === 'button'));
          } catch (e) { return false; }
        })();
      if (!vis(el) && !isFile && !hoverOnly) {
        // Custom-styled radios/checkboxes hide the real input (opacity 0 / sr-only) inside a visible label.
        let ctrl = comboLike ? el.closest('[class*="__control"], [class*="control"], [class*="Control"], [class*="__value-container"]') : null;
        if (!ctrl && choiceLike) { ctrl = el.closest('label'); if (!ctrl && el.id) { try { ctrl = el.ownerDocument.querySelector('label[for="' + CSS.escape(el.id) + '"]'); } catch (e) {} } if (!ctrl) ctrl = el.parentElement; }
        if (!(ctrl && vis(ctrl))) continue;
        posEl = ctrl;
      }
      const role = el.getAttribute('role') || '';
      const item = { ref: ref(el), tag, type, role, label: labelFor(el), bbox: bbox(posEl), inview: inView(posEl) };
      item.required = !!(el.required || el.getAttribute('aria-required') === 'true' || /required|\*/.test(el.getAttribute('aria-label') || '') || starRequired(el));
      item.disabled = !!(el.disabled || el.getAttribute('aria-disabled') === 'true');
      item.invalid = el.getAttribute('aria-invalid') === 'true';
      item.placeholder = el.getAttribute('placeholder') || '';
      // Character caps (Greenhouse short answers 255, SuccessFactors 200): the maxlength attribute, else a hint
      // near the field ("Maximum 500 characters", "0 / 255").
      if ((tag === 'input' && /^(text|email|url|search|)$/.test(type)) || tag === 'textarea' || el.isContentEditable) {
        let mx = (typeof el.maxLength === 'number' && el.maxLength > 0 && el.maxLength < 100000) ? el.maxLength : 0;
        if (!mx) {
          const ff = el.closest('[data-automation-id^="formField"], .field, .form-group, .application-field, [class*="field" i], fieldset') || el.parentElement;
          const ht = ff ? txt(ff).slice(0, 600) : '';
          const m = ht.match(/(?:max(?:imum)?(?:\s+of)?|limit(?:ed)?\s+to|up\s+to|no\s+more\s+than)\s*(\d{2,5})\s*(?:characters|chars)\b/i)
                 || ht.match(/(?:^|\D)(\d{2,5})\s*(?:characters|chars)\s*(?:max(?:imum)?|limit|or\s+(?:less|fewer))\b/i)
                 || ht.match(/(?:^|\D)\d{1,5}\s*\/\s*(\d{2,5})\s*(?:characters|chars)?\s*$/i);
          if (m) mx = parseInt(m[1], 10);
        }
        if (mx) item.max_chars = mx;
      }
      if (tag === 'input' || tag === 'textarea') {
        if (type === 'password') item.value = el.value ? '(set)' : '';
        else if (isFile) {
          item.value = el.files && el.files.length ? el.files[0].name : ''; item.hidden = !vis(el);
          item.name = el.getAttribute('name') || el.id || '';
          // Greenhouse puts a file question's own words in 'upload-label-<id>' (Appian, 2026-10-24: both boxes read
          // 'question_N (Attach)' and the transcript went into the writing-sample box)
          const upq = el.id ? document.getElementById('upload-label-' + el.id) : null;
          const upText = upq ? txt(upq).replace(/\s*\*\s*$/, '').slice(0, 160) : '';
          if (upText) item.label = upText;
          // Portals label hidden file inputs with the button text ("Attach"); the input name says which slot it is.
          else if (item.name && !/resume|cv|cover|transcript/i.test(item.label)) item.label = item.name + ' (' + (item.label || 'file') + ')';
        }
        else if (type === 'checkbox' || type === 'radio') { item.checked = el.checked; }
        else {
          item.value = (el.value || '').slice(0, 300); if ((el.value || '').length > 300) item.value_len = (el.value || '').length;
          if (!item.value && tag === 'input') { const hv = hostPickedValue(el); if (hv) item.shown = hv.slice(0, 200); }
          // the entry a box belongs to, when its name says so: Epic Games' 'Start Date (Year)' (educations[0].start_date.year)
          // took his internship start date from the label alone (2026-10-23)
          const nm = el.getAttribute('name') || '';
          if (/educ|employ|experience|work_?history/i.test(nm)) item.name = nm.slice(0, 80);
        }
        {
          // A Workday prompt's selected chips are its value, even when its search box still holds typed text
          // (AeroVironment's Certification read 'Cloud' over a picked 'AWS Certified Cloud Practitioner - AWS', 2026-10-19).
          const ms = el.closest('[data-automation-id="multiSelectContainer"]');
          const picked = ms ? Array.from(ms.querySelectorAll('[data-automation-id="selectedItem"]')).map(x => txt(x)).filter(Boolean) : [];
          if (picked.length) { item.value = picked.join('; ').slice(0, 300); item.selected_items = picked.length; }
        }
        const isCombo = role === 'combobox' || el.getAttribute('aria-autocomplete') || el.hasAttribute('aria-expanded') || /react-select|__input/i.test(el.id || el.className || '');
        // Oracle's cx-select input holds its own pick; the text around it is the label and error text, never a value
        // (onsemi's empty Start Date Month read 'Month Year', 2026-10-19)
        const ownValue = /cx-select-input/.test(String(el.className || ''));
        if (isCombo && !item.value && !ownValue) {
          let c = el.closest('[class*="__control"], [class*="control"], [class*="Control"], [class*="__value-container"]');
          if (!c || !txt(c)) { c = null; let p = el.parentElement; for (let i = 0; i < 5 && p; i++, p = p.parentElement) { const t = txt(p); if (t && t.length < 240) { c = p; break; } } }
          let shown = c ? txt(c) : '';
          // the walk up can reach the field's own box (L3Harris, 2026-10-21: an empty SuccessFactors dropdown read
          // '* Gender' once its icon glyph was gone): its label texts are not a pick, however long the question
          if (c && shown) { for (const l of c.querySelectorAll('label, legend')) { const lt = txt(l); if (lt) shown = shown.replace(lt, ''); } }
          // a screen-reader status line is no pick: SuccessFactors' paginated picklist keeps 'One or more results available.
          // Press Up or Down Arrow Keys ...' in its box, and Westinghouse's empty Graduation Year read as that (2026-10-23)
          if (c && shown) { for (const s of c.querySelectorAll('[role="status"], [aria-live], .sr-only, .visually-hidden')) { const st = txt(s); if (st) shown = shown.replace(st, ''); } }
          // the label is taken off only where it leads the text: 'Degree' is also inside "Bachelor's Degree", and DriveWealth's
          // pick read back as "Bachelor's" through five submits (2026-10-21)
          if (shown && item.label && shown.trim().startsWith(item.label)) shown = shown.trim().slice(item.label.length);
          shown = shown.replace(/^[\s*:]+|[\s*:]+$/g, '').trim();
          if (shown && !PLACEHOLDER.test(shown)) item.shown = shown.slice(0, 200);
          item.combo = true;
        }
        if (isCombo && ownValue) item.combo = true;
        // A Workday prompt ("Search", "0 items selected") only keeps an option picked from its list: show it as a
        // combobox so it is worked with choose (Aerospace Corporation's Field of Study, 2026-10-19).
        if (!item.combo && (el.getAttribute('data-uxi-widget-type') === 'selectinput' || el.closest('[data-automation-id="multiSelectContainer"]'))) item.combo = true;
        if (type === 'checkbox' || type === 'radio') {
          const key = (el.name ? 'n:' + el.name : 'g:' + (groupLabel(el) || item.label)) + '|' + (root === document ? '' : 'r');
          if (!groups.has(key)) groups.set(key, { kind: type, label: groupLabel(el), required: item.required, options: [], invalid: false });
          const g = groups.get(key); g.options.push({ ref: item.ref, label: item.label, checked: el.checked, bbox: item.bbox }); g.required = g.required || item.required; g.invalid = g.invalid || item.invalid;
          continue;
        }
      } else if (tag === 'select') {
        item.value = el.selectedOptions.length ? txt(el.selectedOptions[0]) : '';
        item.options = Array.from(el.options).map(o => txt(o)).filter(Boolean).slice(0, 25);
        item.noptions = el.options.length;
      } else if (el.classList && el.classList.contains('beautiful-timeline-item')) {
        // Oracle's Work and Education History timeline: each entry opens on a click and shows its Edit / Remove only then
        // (Vertiv, 2026-10-21: 'Unnamed Job Title' could not be reached and the run held)
        item.tag = 'button';
        const tt = txt(el.querySelector('.beautiful-timeline-item__title'));
        const dr = ((el.querySelector('.oj-helper-hidden-accessible') || {}).textContent || '').replace(/\s+/g, ' ').trim();
        item.label = ('Entry: ' + tt + (dr ? ' (' + dr + ')' : '')).slice(0, 160);
      } else if (tag === 'button' || role === 'button' || el.hasAttribute('aria-haspopup') || el.getAttribute('data-automation-id') === 'selectWidget') {
        const shown = txt(el).slice(0, 120);
        // aria-haspopup="dialog" opens a dialog, not a list: SuccessFactors' 'Read and accept the data privacy acknowledgment
        // statement' was a dropdown here, click refused it as a choice control, and account creation parked (Westinghouse,
        // 2026-10-22)
        const popup = (el.getAttribute('aria-haspopup') || '').toLowerCase();
        const isDropdown = (el.hasAttribute('aria-haspopup') && popup !== 'dialog' && popup !== 'false')
          || el.getAttribute('data-automation-id') === 'selectWidget' || role === 'combobox';
        if (isDropdown) {
          item.tag = 'dropdown';
          const aria = el.getAttribute('aria-label') || '';
          const cleanAria = aria.replace(/\s*(Select One|Required|selected)\s*/gi, ' ').replace(/\s+/g, ' ').trim();
          let fieldLabel = '';
          const ff = el.closest('[data-automation-id^="formField"], .field, .form-group, fieldset, [class*="field" i]');
          if (ff) { const l = ff.querySelector('label, legend, [class*="label" i]'); if (l && !l.contains(el)) fieldLabel = txt(l); }
          item.label = fieldLabel || cleanAria || (item.label !== shown ? item.label : '') || shown;
          item.value = PLACEHOLDER.test(shown) ? '' : shown;
          item.required = item.required || /required/i.test(aria);
        } else {
          item.label = shown || item.label;
          if (!item.label) continue;
          // an entry tile's bare Edit / Delete is named by its tile ('Delete: Engineering Honors / Texas A&M University'):
          // the outline listed five Deletes with no name and no Edit at all (AutoZone, 2026-10-21)
          const tileEl = el.closest('article.apply-flow-profile-item-tile, .beautiful-timeline-item');
          if (tileEl && /^(edit|delete|remove)$/i.test(item.label.trim())) {
            const tt = txt(tileEl.querySelector('.apply-flow-profile-item-tile__summary-title, .beautiful-timeline-item__title'));
            const ts = txt(tileEl.querySelector('.apply-flow-profile-item-tile__summary-subtitle')) ||
              ((tileEl.querySelector('.oj-helper-hidden-accessible') || {}).textContent || '').replace(/\s+/g, ' ').trim();
            const nm = [tt, ts].filter(Boolean).join(' / ');
            if (nm) item.label = item.label.trim() + ': ' + nm.slice(0, 140);
          }
          // a shadow-root button is named by its host: its aria-label and its visible text both count
          const names = [item.label].concat(shown ? [] : shadowHostNames(el)).filter((n, i, a) => n && a.indexOf(n) === i);
          const nFilled = Array.from(document.querySelectorAll('input:not([type=hidden]):not([type=file]):not([type=checkbox]):not([type=radio]):not([type=submit]), textarea')).filter(i => (i.value || '').trim() && i.getBoundingClientRect().width > 0 && i.getBoundingClientRect().height > 0).length;
          const onAccountPage = Array.from(document.querySelectorAll('input[type=password]')).some(i => { const r = i.getBoundingClientRect(); return r.width > 0 && r.height > 0; });
          const submitName = names.some(n => /^(submit|submit application|submit my application|send application|finish|complete application|complete)$/i.test(n)
            || (/^(apply|apply now|apply for this job|apply to this job)$/i.test(n) && nFilled >= 3));
          const accountBtn = onAccountPage || (!submitName && names.some(n => /^(create account|create an account|sign in|sign-in|log in|login|continue|next|save and continue|save & continue|verify|verify email|send code|resend|back)$/i.test(n)));
          // a modal's own Save (ApplicantPro's contact-information popup, SimonComputing 2026-10-22) is no application submit
          const inDialog = !!(el.closest && el.closest('dialog, [role="dialog"], [role="alertdialog"], [aria-modal="true"], [class*="modal" i], [class*="dialog" i], [class*="popup" i], [class*="popover" i]'));
          // a message to the employer IS the application: Work at a Startup's 'Reach out to Akash at Dime' dialog was sent with
          // a plain click, past the gate and the judge (2026-10-25)
          const dlg = el.closest && el.closest('dialog, [role="dialog"], [aria-modal="true"], [class*="modal" i], form');
          const sendMsg = names.some(n => /^(send|send message|send note|send intro|send introduction|send request)$/i.test(n))
            && !!dlg && Array.from(dlg.querySelectorAll('textarea')).some(t => (t.value || '').trim().length >= 20);
          item.submitish = !accountBtn && (submitName || sendMsg || (type === 'submit' && nFilled >= 3 && !inDialog));
          // Toggle buttons (Ashby Yes/No, segmented choices) carry aria-pressed/aria-checked: an answer, not navigation.
          const pressed = el.getAttribute('aria-pressed') ?? el.getAttribute('aria-checked');
          if (pressed !== null) {
            item.tag = 'toggle'; item.pressed = pressed === 'true';
            item.group = groupLabel(el) || (() => { let p = el.parentElement; for (let i = 0; i < 4 && p; i++, p = p.parentElement) { const t = txt(p); if (t.length > item.label.length + 4 && t.length < 400) return t.replace(item.label, '').replace(/\s+/g, ' ').trim().slice(0, 200); } return ''; })();
          }
        }
      } else if ((role === 'radio' || role === 'checkbox' || role === 'switch') && el.hasAttribute('aria-checked')) {
        // ARIA radios drawn with spans (SAP SuccessFactors' radioContainer, CenturyLink 2026-10-21: four required
        // questions showed only 'Yes' / 'No' items without their question, none was set, and the gate passed): a toggle
        // that carries its question, so it is answered like Ashby's Yes / No buttons.
        item.tag = 'toggle'; item.pressed = el.getAttribute('aria-checked') === 'true';
        // an option drawn as a div with its own text ('Yes' / 'No') is named by that text, and the question it answers
        // goes to the group: Rippling's options both read as the question, and 'Yes' was pressed for "Do you require
        // work authorization to work in the United States?" (2026-10-21)
        const ownText = txt(el);
        if (ownText && ownText.length <= 80 && ownText !== item.label) { item.question0 = item.label; item.label = ownText; }
        let rg = el.closest('[role="radiogroup"], [role="group"]');
        // SmartRecruiters' <spl-radio> sits in its group host's light DOM and shows through a slot in the group's
        // shadow <fieldset role="radiogroup">, whose label holds the question in another slot (CapTech, 2026-10-21)
        if (!rg && el.assignedSlot) rg = el.assignedSlot.closest('[role="radiogroup"], [role="group"]');
        const lb = rg ? (rg.getAttribute('aria-labelledby') || '') : '';
        let q = '';
        if (lb) { const rr = rg.getRootNode ? rg.getRootNode() : null; const id0 = lb.split(/\s+/)[0];
          const n = (rr && rr.getElementById ? rr.getElementById(id0) : null) || el.ownerDocument.getElementById(id0); if (n) q = ctxt(n); }
        if (!q && rg) q = (rg.getAttribute('aria-label') || '').trim();
        item.group = (q || groupLabel(el) || item.question0 || '').slice(0, 200);
        if (item.group && item.group === item.label && item.question0) item.group = item.question0.slice(0, 200);
        delete item.question0;
        if (rg && (rg.getAttribute('aria-required') === 'true' || /^\s*\*|\*\s*$/.test(q))) item.required = true;
      } else if (tag === 'a') {
        item.label = txt(el).slice(0, 100);
        if (!item.label || !inView(el)) continue;
        item.href = (el.getAttribute('href') || '').slice(0, 120);
      } else if (el.getAttribute('contenteditable') === 'true') {
        item.tag = 'richtext'; item.value = txt(el).slice(0, 300);
      } else if (role === 'option' || el.getAttribute('data-automation-id') === 'promptOption') {
        item.tag = 'option'; item.label = txt(el).slice(0, 120); item.selected = el.getAttribute('aria-selected') === 'true';
      } else {
        item.label = item.label || txt(el).slice(0, 100);
        // Workday's selected-items list holds the prompt's picks as chips, and they are its value: Itron's read-back paired
        // the How Did You Hear decision with this list, found it empty although 'Job Posting' was picked, and blocked the
        // page (2026-10-21).
        if (el.getAttribute('data-automation-id') === 'selectedItemList') {
          const chips = Array.from(el.querySelectorAll('[data-automation-id="selectedItem"]')).map(x => txt(x)).filter(Boolean);
          item.value = chips.join('; ').slice(0, 300);
          if (chips.length) item.selected_items = chips.length;
        }
      }
      items.push(item);
    }
  }
  // A lone checkbox (consent, attestation) is a field of its own, not a group.
  const groupList = [];
  for (const g of groups.values()) {
    if (g.kind === 'checkbox' && g.options.length === 1) {
      const o = g.options[0];
      items.push({ ref: o.ref, tag: 'input', type: 'checkbox', role: '', label: o.label || g.label, bbox: o.bbox, inview: true, required: g.required, invalid: g.invalid, checked: o.checked });
    } else {
      if (!g.label && g.options.length) g.label = g.options.map(o => o.label).join(' / ').slice(0, 120);
      groupList.push(g);
    }
  }
  // Oracle's entry tiles: what each one says and its Edit / Delete refs (the gate checks them against his stored entries)
  const tiles = [];
  // hidden tiles too: while an entry form is open Oracle hides the others, and the gate passed AutoZone with three
  // wrong employers out of sight (2026-10-21). A hidden tile with no form open is on another step of the form (GM
  // Financial's Experience step, seen from step 4): it is marked so the gate sends the run back to that step.
  const formOpenNow = Array.from(document.querySelectorAll('.profile-item-content--form')).some(f => vis(f));
  for (const t of document.querySelectorAll('article.apply-flow-profile-item-tile')) {
    if (tiles.length >= 20) continue;
    const blk = t.closest('.apply-flow-block__form-list') || (t.parentElement && t.parentElement.closest('[aria-label]'));
    const sec = ((blk && blk.getAttribute('aria-label')) || '').replace(/\s*Required\s*$/i, '').trim();
    const ed = t.querySelector('button[class*="edit" i]'); const de = t.querySelector('button[class*="delete" i]');
    tiles.push({ section: sec.slice(0, 60), title: txt(t.querySelector('.apply-flow-profile-item-tile__summary-title')).slice(0, 120),
      sub: txt(t.querySelector('.apply-flow-profile-item-tile__summary-subtitle')).slice(0, 160), invalid: /--invalid/.test(String(t.className || '')),
      edit: ed ? (ed.getAttribute('data-goat-ref') || '') : '', del: de ? (de.getAttribute('data-goat-ref') || '') : '',
      visible: vis(t) || formOpenNow });
  }
  for (const t of document.querySelectorAll('.beautiful-timeline-item[data-profile-item-tile]')) {
    if (tiles.length >= 20) break;
    const cls = String(t.className || '');
    const sec = /--experien/i.test(cls) ? 'Work History' : (/--educ/i.test(cls) ? 'Education' : '');
    const subEl = t.querySelector('.beautiful-timeline-item__subtitle, [class*="timeline-item__subtitle"]');
    tiles.push({ section: sec, title: txt(t.querySelector('.beautiful-timeline-item__title')).slice(0, 120),
      sub: subEl ? txt(subEl).slice(0, 160) : '', dates: ((t.querySelector('.oj-helper-hidden-accessible') || {}).textContent || '').replace(/\s+/g, ' ').trim().slice(0, 80),
      invalid: t.getAttribute('data-profile-item-invalid') === 'true', edit: t.getAttribute('data-goat-ref') || '', del: '', timeline: true,
      visible: vis(t) || formOpenNow });
  }
  const headings = [];
  for (const h of document.querySelectorAll('h1, h2, h3, [role="heading"]')) { if (vis(h)) { const t = txt(h); if (t && t.length < 160) headings.push(t); } if (headings.length >= 12) break; }
  const bodyText = (document.body ? document.body.innerText : '').replace(/\s+/g, ' ');
  return JSON.stringify({ url: location.href, title: document.title, headings, items: items.slice(0, maxItems),
    groups: groupList, tiles, entry_form_open: Array.from(document.querySelectorAll('.profile-item-content--form')).some(f => vis(f)),
    required_blocks: Array.from(document.querySelectorAll('.apply-flow-block__form-list[aria-label]')).map(b => b.getAttribute('aria-label') || '')
      .filter(l => /\bRequired\s*$/i.test(l)).map(l => l.replace(/\s*Required\s*$/i, '').trim()).slice(0, 12),
    total: items.length, scrollY: Math.round(scrollY), pageHeight: Math.round(document.documentElement.scrollHeight), vw: innerWidth, vh: innerHeight,
    text_head: bodyText.slice(0, 600) });
}
"""

# --------------------------------------------------------------------------- element at point
JS_ELEMENT_AT = r"""
([x, y]) => {
""" + _COMMON + r"""
  const SUBMIT = /^(submit|submit application|submit my application|send application|finish|complete application|complete|send)$/i;
  // "Apply" on a posting page opens the form; it is a submit control only once the form holds answers.
  const APPLY = /^(apply|apply now|apply for this job|apply to this job)$/i;
  const filledInputs = Array.from(document.querySelectorAll('input:not([type=hidden]):not([type=file]):not([type=checkbox]):not([type=radio]):not([type=submit]), textarea')).filter(i => (i.value || '').trim() && i.getBoundingClientRect().width > 0 && i.getBoundingClientRect().height > 0).length;
  const formFilled = filledInputs >= 3;
  // A visible password box means a sign-in / create-account page: its "Submit" button (Workday lays a hidden
  // "Submit" exactly over "Sign In") signs in; it never submits an application.
  // A password-help / reset page has no password box (SAP SuccessFactors' 'Password Help', Phillips 66, 2026-10-21): its
  // title or heading says what it is, and its 'Submit' requests the reset email.
  const accountPage = Array.from(document.querySelectorAll('input[type=password]')).some(i => { const r = i.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
    || /forgot (your )?password|reset (your )?password|password help|change (your )?password|candidate login|please review before continuing/i.test(document.title + ' ' + Array.from(document.querySelectorAll('h1,h2,h3')).map(h => (h.innerText || '')).join(' '));
  const CAPTCHA_SRC = /recaptcha|hcaptcha|turnstile|challenges\.cloudflare|arkoselabs|funcaptcha|captcha/i;
  let el = document.elementFromPoint(x, y);
  let hops = 0;
  while (el && el.shadowRoot && hops < 5) { const inner = el.shadowRoot.elementFromPoint(x, y); if (!inner || inner === el) break; el = inner; hops++; }
  if (!el) return JSON.stringify({ found: false });
  const out = { found: true, tag: el.tagName.toLowerCase(), text: txt(el).slice(0, 120), id: el.id || '', cls: (el.className || '').toString().slice(0, 120), ref: el.getAttribute('data-goat-ref') || '' };
  let p = el; let submit = false, captcha = false, why = '';
  for (let i = 0; p && i < 8; i++, p = p.parentElement) {
    const tag = p.tagName.toLowerCase();
    const type = (p.getAttribute('type') || '').toLowerCase();
    const t = txt(p).slice(0, 80);
    // a custom element that draws a native button in its shadow root (SmartRecruiters' spl-button): a click on its
    // slotted text lands on the host, which is the button as far as the page is concerned
    const isBtn = tag === 'button' || tag === 'a' || p.getAttribute('role') === 'button' || (tag === 'input' && (type === 'submit' || type === 'button'))
      || !!(p.shadowRoot && p.shadowRoot.querySelector('button, [role="button"]'));
    if (isBtn) {
      const own = (tag === 'input' ? (p.value || '') : (t || p.getAttribute('aria-label') || '')).replace(/\s+/g, ' ').trim();
      // a bare <button> inside a custom element's shadow root is named by its host (SmartRecruiters' spl-button)
      const names = own ? [own] : shadowHostNames(p);
      const label = names[0] || '';
      // Account-flow buttons (Create Account, Sign In, Continue, Verify) are navigation, even when type=submit.
      const ACCOUNT = /^(create account|create an account|sign in|sign-in|log in|login|continue|next|save and continue|save & continue|verify|verify email|send code|resend|back)$/i;
      const submitName = names.some(n => SUBMIT.test(n) || (APPLY.test(n) && formFilled));
      if (!submitName && names.some(n => ACCOUNT.test(n))) break;
      // a dialog's own Send / Save / OK (ADP's 'send a verification code' dialog, Mathtech 2026-10-22) is no application submit
      if (p.closest && p.closest('dialog, [role="dialog"], [role="alertdialog"], [aria-modal="true"], [class*="modal" i], [class*="dialog" i], [class*="popup" i], [class*="popover" i]')
          && names.some(n => /^(send|save|ok|done|confirm|send code|send verification code)$/i.test(n))) break;
      // ... unless the account form IS the application (SuccessFactors quick apply: Safran's 'Apply' sent it, 2026-10-22)
      const quickApply = names.some(n => /^(apply|apply now|submit|submit application|submit my application|send application)$/i.test(n))
        && Array.from(document.querySelectorAll('input:not([type=hidden]):not([type=file]):not([type=checkbox]):not([type=radio]):not([type=submit]):not([type=password]), textarea')).filter(i => (i.value || '').trim() && i.getBoundingClientRect().width > 0 && i.getBoundingClientRect().height > 0).length >= 6;
      if (accountPage && !quickApply) break;
      if (quickApply) { submit = true; why = 'submit control (application on an account page): ' + label; break; }
      if (submitName || (type === 'submit' && formFilled)) { submit = true; why = 'submit control: ' + label; }
      // Simplify's panel "Submit Application" is also a submit control; any extension panel is.
      break;
    }
    if (tag === 'iframe' && CAPTCHA_SRC.test(p.getAttribute('src') || '')) { captcha = true; why = 'captcha frame'; }
    if (/g-recaptcha|h-captcha|cf-turnstile|captcha|challenge/i.test((p.className || '').toString() + ' ' + (p.id || ''))) { if (!/badge/i.test((p.className || '').toString())) { captcha = true; why = 'captcha widget'; } }
  }
  if (out.tag === 'iframe' && CAPTCHA_SRC.test(el.getAttribute('src') || '')) { captcha = true; why = 'captcha frame'; }
  out.submit = submit; out.captcha = captcha; out.why = why;
  return JSON.stringify(out);
}
"""

# Whether a click at (x, y) reaches the element [data-goat-ref=ref]: 'ok', 'covered:<tag>' (something else is on top,
# such as a password manager's inline menu), or 'missing' (not in this document, e.g. inside a frame).
# A cookie / privacy layer over the page (Texas Instruments' Tealium wall, 2026-10-20: "Click Agree and Proceed to accept
# cookies and enter the site" lay over the whole Oracle form, so every picker click landed on it). Returns null when there
# is none; otherwise clicks its decline / necessary-only button, or, only when the layer blocks the page and offers no
# decline, its accept button.
JS_COOKIE_WALL = r"""
() => {
  const vw = innerWidth, vh = innerHeight;
  const vis = e => { const r = e.getBoundingClientRect(); const cs = getComputedStyle(e);
                     return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none' && cs.opacity !== '0'; };
  const COOKIE = /\b(cookies?|tracking technologies|privacy preferences)\b/i;
  const DECLINE = /^(reject all|reject|decline all|decline|refuse all|refuse|deny all|deny|necessary only|only necessary|strictly necessary only|essential only|only essential|use necessary cookies only|accept necessary|accept only necessary|accept required only|required only|continue without accepting|do not accept|no,? thanks)\b/i;
  const ACCEPT = /^(agree and proceed|accept all cookies|accept all|accept cookies|accept and close|accept & close|accept|allow all cookies|allow all|allow cookies|i agree|agree|got it|ok|okay|i understand|understood)\b/i;
  let best = null;
  for (const el of document.querySelectorAll('div, section, aside, dialog, [role=dialog], [role=alertdialog], footer')) {
    const cs = getComputedStyle(el);
    const layer = cs.position === 'fixed' || cs.position === 'sticky' || el.tagName === 'DIALOG' || /dialog/.test(el.getAttribute('role') || '');
    if (!layer || !vis(el)) continue;
    const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
    if (!t || t.length > 3000 || !COOKIE.test(t)) continue;
    const r = el.getBoundingClientRect();
    const area = Math.max(0, Math.min(r.right, vw) - Math.max(r.left, 0)) * Math.max(0, Math.min(r.bottom, vh) - Math.max(r.top, 0));
    if (!best || area > best.area) best = { el, area };
  }
  if (!best) return null;
  const el = best.el;
  const mid = document.elementFromPoint(vw / 2, vh / 2);
  const blocking = !!(mid && el.contains(mid)) || best.area >= 0.35 * vw * vh;
  const buttons = Array.from(el.querySelectorAll('button, a, [role=button], input[type=button], input[type=submit]')).filter(vis)
    .map(b => ({ b, t: (b.innerText || b.value || b.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim() }))
    .filter(x => x.t && x.t.length <= 60);
  const dec = buttons.find(x => DECLINE.test(x.t));
  const acc = buttons.find(x => ACCEPT.test(x.t));
  const pick = dec || (blocking ? acc : null);
  const names = buttons.map(x => x.t).slice(0, 8);
  if (!pick) return { found: true, blocking, clicked: null, buttons: names };
  pick.b.click();
  return { found: true, blocking, clicked: pick.t, action: dec ? 'decline' : 'accept', buttons: names };
}
"""

# Focus a field directly when it does not hold the focus already (it, or an element inside it).
JS_FOCUS_REF = r"""
(ref) => {
""" + _COMMON + r"""
  const t = findRef(ref);
  if (!t) return false;
  // inside a shadow root the document's activeElement is the host: ask the element's own root
  const root = t.getRootNode ? t.getRootNode() : document;
  const active = () => (root && 'activeElement' in root) ? root.activeElement : document.activeElement;
  const a = active();
  if (a === t || (a && t.contains(a))) return true;
  if (t.focus) t.focus();
  return active() === t;
}
"""

JS_HIT_TARGET = r"""
([ref, x, y]) => {
""" + _COMMON + r"""
  const t = findRef(ref);
  if (!t) return 'missing';
  let at = document.elementFromPoint(x, y);
  let hops = 0;
  while (at && at.shadowRoot && hops < 5) { const inner = at.shadowRoot.elementFromPoint(x, y); if (!inner || inner === at) break; at = inner; hops++; }
  if (!at) return 'none';
  if (at === t || t.contains(at) || at.contains(t)) return 'ok';
  // A shadow-DOM button shows its label through a <slot>: the text is light-DOM content of the host, so the point hits
  // that span and t.contains() is false (SmartRecruiters' spl-button 'Next', ServiceNow 2026-10-21: every Next click
  // read as covered and went nowhere). Walking the composed tree (slot, parent, shadow host) finds the button.
  for (let p = at, i = 0; p && i < 40; i++) { if (p === t) return 'ok'; p = p.assignedSlot || p.parentNode || p.host || null; }
  const lab = at.closest ? at.closest('label') : null;
  if (lab && (lab.control === t || lab.contains(t))) return 'ok';
  const root = t.getRootNode ? t.getRootNode() : null;
  if (root && root.host && (root.host === at || at.contains(root.host))) return 'ok';
  // The page's own control on top is part of the page: Workday lays a transparent 'Submit' button exactly over
  // 'Sign In', and that button is what signs in. The normal click (with its submit guard) handles it.
  for (let p = at, i = 0; p && i < 4; i++, p = p.parentElement) {
    const tag = (p.tagName || '').toLowerCase();
    const role = (p.getAttribute && p.getAttribute('role')) || '';
    if (['button', 'a', 'input', 'select', 'textarea', 'label'].includes(tag) || role === 'button' || role === 'link') return 'ok:control';
  }
  return 'covered:' + (at.tagName || '').toLowerCase() + (at.id ? '#' + at.id : '');
}
"""

# A direct click on the element [data-goat-ref=ref], found in any shadow root too. True when it was there to click.
JS_CLICK_REF = r"""
(ref) => {
""" + _COMMON + r"""
  const t = findRef(ref);
  if (!t) return false;
  t.click();
  return true;
}
"""

# The submit / captcha rules of JS_ELEMENT_AT applied to the element [data-goat-ref=ref] itself (for a click that
# cannot go through the point because something covers it).
JS_TARGET_GUARD = r"""
(ref) => {
""" + _COMMON + r"""
  const SUBMIT = /^(submit|submit application|submit my application|send application|finish|complete application|complete|send)$/i;
  const APPLY = /^(apply|apply now|apply for this job|apply to this job)$/i;
  const ACCOUNT = /^(create account|create an account|sign in|sign-in|log in|login|continue|next|save and continue|save & continue|verify|verify email|send code|resend|back|reset password|send reset link|submit)$/i;
  const t = findRef(ref);
  if (!t) return JSON.stringify({ found: false });
  const filledInputs = Array.from(document.querySelectorAll('input:not([type=hidden]):not([type=file]):not([type=checkbox]):not([type=radio]):not([type=submit]), textarea')).filter(i => (i.value || '').trim() && i.getBoundingClientRect().width > 0 && i.getBoundingClientRect().height > 0).length;
  const formFilled = filledInputs >= 3;
  const accountPage = Array.from(document.querySelectorAll('input[type=password]')).some(i => { const r = i.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
    || /forgot (your )?password|reset (your )?password|password help|change (your )?password|create (an |your )?account|sign in|candidate login|please review before continuing/i.test(document.title + ' ' + Array.from(document.querySelectorAll('h1,h2,h3')).map(h => txt(h)).join(' '));
  let submit = false, captcha = false, why = '';
  for (let p = t, i = 0; p && i < 8; i++, p = p.parentElement) {
    const tag = p.tagName.toLowerCase();
    const type = (p.getAttribute('type') || '').toLowerCase();
    // a custom element that draws a native button in its shadow root (SmartRecruiters' spl-button): a click on its
    // slotted text lands on the host, which is the button as far as the page is concerned
    const isBtn = tag === 'button' || tag === 'a' || p.getAttribute('role') === 'button' || (tag === 'input' && (type === 'submit' || type === 'button'))
      || !!(p.shadowRoot && p.shadowRoot.querySelector('button, [role="button"]'));
    if (isBtn) {
      const own = (tag === 'input' ? (p.value || '') : (txt(p) || p.getAttribute('aria-label') || '')).replace(/\s+/g, ' ').trim();
      const names = own ? [own] : shadowHostNames(p);
      const label = names[0] || '';
      const submitName = names.some(n => SUBMIT.test(n) || (APPLY.test(n) && formFilled));
      if (accountPage && names.some(n => ACCOUNT.test(n))) break;
      // a dialog's own Send / Save / OK (ADP's 'send a verification code' dialog, Mathtech 2026-10-22) is no application submit
      if (p.closest && p.closest('dialog, [role="dialog"], [role="alertdialog"], [aria-modal="true"], [class*="modal" i], [class*="dialog" i], [class*="popup" i], [class*="popover" i]')
          && names.some(n => /^(send|save|ok|done|confirm|send code|send verification code)$/i.test(n))) break;
      if (!submitName && names.some(n => /^(create account|create an account|sign in|sign-in|log in|login|continue|next|save and continue|save & continue|verify|back)$/i.test(n))) break;
      // ... unless the account form IS the application (SuccessFactors quick apply: Safran's 'Apply' sent it, 2026-10-22)
      const quickApply = names.some(n => /^(apply|apply now|submit|submit application|submit my application|send application)$/i.test(n))
        && Array.from(document.querySelectorAll('input:not([type=hidden]):not([type=file]):not([type=checkbox]):not([type=radio]):not([type=submit]):not([type=password]), textarea')).filter(i => (i.value || '').trim() && i.getBoundingClientRect().width > 0 && i.getBoundingClientRect().height > 0).length >= 6;
      if (accountPage && !quickApply) break;
      if (quickApply) { submit = true; why = 'submit control (application on an account page): ' + label; break; }
      if (submitName || (type === 'submit' && formFilled)) { submit = true; why = 'submit control: ' + label; }
      break;
    }
    if (/g-recaptcha|h-captcha|cf-turnstile|captcha|challenge/i.test((p.className || '').toString() + ' ' + (p.id || ''))) { captcha = true; why = 'captcha widget'; }
  }
  return JSON.stringify({ found: true, submit, captcha, why, text: txt(t).slice(0, 80) });
}
"""

# --------------------------------------------------------------------------- page errors (Hermes, proven)
JS_PAGE_ERRORS = r"""
() => {
  // A field whose own label ends in "*" is required even without a required / aria-required attribute (Epic Games'
  // "Email*:", "School*:", "Gender Identity*:", 2026-10-19: the gate passed with all of them blank). The label is the
  // first plain label / legend above the control inside its own field box; a box holding other fields ends the search.
  const STAR_RE = /\*\s*:?\s*$/;
  const zw = (s) => (s || '').replace(/[\u200B-\u200D\u2060\uFEFF]/g, '').replace(/\s+/g, ' ').trim();
  // The first plain label / legend above a control inside its own field box (a box holding other fields ends the
  // search): Epic Games keeps "<label>Email*:</label>" in a sibling block whose for= names no element.
  function boxHead(el) {
    const choice = el.type === 'radio' || el.type === 'checkbox';
    let p = el.parentElement;
    for (let i = 0; i < 12 && p && p !== document.body; i++, p = p.parentElement) {
      const others = Array.from(p.querySelectorAll('input:not([type="hidden"]), select, textarea'))
        .filter(f => f !== el && !(choice && f.name && f.name === el.name));
      if (others.some(f => { const r = f.getBoundingClientRect(); return r.width > 0 && r.height > 0; })) return null;
      const heads = Array.from(p.querySelectorAll('label, legend')).filter(l => !l.contains(el) && !l.querySelector('input, select, textarea'));
      if (heads.length) return heads[0];
    }
    return null;
  }
  function starRequired(el) {
    const choice = el.type === 'radio' || el.type === 'checkbox';
    const own = el.labels ? Array.from(el.labels) : [];
    if (!choice && own.length) return own.some(l => STAR_RE.test(zw(l.innerText || l.textContent)));
    const h = boxHead(el);
    return !!(h && STAR_RE.test(zw(h.innerText || h.textContent)));
  }

  const ERR = /(no\s+\S+\s+found|not\s+a\s+valid|invalid|must\s+be|must\s+contain|cannot\s+be\s+(blank|empty)|can't\s+be\s+(blank|empty)|is\s+required|this\s+field\s+is\s+required|please\s+(enter|select|provide|choose|complete|correct|fix)|try\s+(entering|again|a\s+different)|failed|too\s+(short|long)|does\s+not\s+match|already\s+(exists|taken|registered|in\s+use)|incorrect|wrong\s+(email|password|username)|(account|user)\s+(might\s+be|is|has\s+been)\s+locked|unable\s+to\s+(sign|log)\s+in|could\s+not\s+(sign|log)\s+in)/i;
  const SKIP = /^(required|\*|optional|required\s*\*|\*\s*required)$/i;
  // "Please correct highlighted fields" is a banner about the form, not about the field above it (Epic Games, 2026-10-19)
  const FORM_SUMMARY = /(missing\s+entry\s+for\s+required\s+field|your\s+form\s+needs\s+corrections|please\s+(complete|fill\s+in)\s+(all\s+)?(the\s+)?required\s+fields|(fix|correct)\s+the\s+(errors|following)\s+(below|above|before)|(please\s+)?correct\s+(the\s+)?highlighted\s+fields?)/i;
  const STATUSY = /(loaded|loading|please\s+wait|saved|updated|added|removed|expanded|collapsed|in\s+progress|step\s+\d|page\s+\d|results?\s+(found|updated)|showing\s+\d|selected)/i;
  // ... and a paragraph that explains what a question is for (Rippling, 2026-10-21: "Our internships are designed for
  // students ... Please provide your anticipated graduation date in order to access your eligibility" blocked every submit)
  // ... and an employer's policy note under a question (Crusoe, 2026-10-21: "Crusoe is not sponsoring visas for these
  // positions ... Candidates must be legally authorized to work" under the sponsorship question blocked every submit)
  const INSTRUCTIONAL = /(if\s+applicable|e\.g\.|for\s+example|\(optional\)|as\s+it\s+appears|do\s+not\s+include|maximum\s+of|up\s+to\s+\d|characters?\s+remaining|format:|in\s+order\s+to|(are|is)\s+designed\s+for|for\s+(demographic|outreach|statistical|reporting)\s+purposes|(is|are)\s+not\s+(sponsoring|able\s+to\s+sponsor|offering\s+sponsorship)|(does|do|will)\s+not\s+(sponsor|provide\s+sponsorship)|(candidates?|applicants?)\s+must|to\s+conform\s+(to|with)\s+(u\.?\s?s\.?\s+)?(government\s+)?(export|federal))/i;
  const alerts = [], errorText = [], emptyRequired = [], summaries = [];
  const seenRoots = new Set(), seenText = new Set();
  function vis(el) { try { const r = el.getBoundingClientRect(); const s = el.ownerDocument.defaultView.getComputedStyle(el);
      return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && parseFloat(s.opacity || '1') > 0.01; } catch (e) { return false; } }
  function txt(el) { return (el.innerText || el.textContent || '').replace(/[\u200B-\u200D\u2060\uFEFF\uE000-\uF8FF]/g, '').replace(/\s+/g, ' ').trim(); }
  // The question a field belongs to: its own label when that says something, else the nearest question text above it
  // (Greenhouse's file questions label the hidden input just "Attach").
  function questionOf(field) {
    let lab = null;
    try { if (field.id) lab = document.querySelector('label[for="' + CSS.escape(field.id) + '"]'); } catch (e) { lab = null; }
    const own = lab ? txt(lab) : '';
    if (own && !/^(attach|upload|browse|choose file|select file)$/i.test(own.trim())) return own.slice(0, 120);
    let p = field.parentElement;
    for (let i = 0; i < 6 && p; i++, p = p.parentElement) {
      const q = p.querySelector('label:not(.visually-hidden), legend, [class*="label" i]:not(.visually-hidden)');
      if (q && !q.contains(field)) { const t = txt(q); if (t && !/^(attach|upload|browse)$/i.test(t.trim())) return t.slice(0, 160); }
    }
    return own.slice(0, 120);
  }
  function nearestLabel(el) {
    // An error tied to its field by id ("question_123-error" for "question_123", aria-errormessage) names THAT field,
    // not whichever label happens to sit nearby (Stoke Space, 2026-10-18: a required essay upload's error was read as
    // the work-authorization question's, and the run kept re-setting the wrong field).
    try {
      const m = el.id && el.id.match(/^(.+?)[-_](error|errors|err|help)$/i);
      const owner = (m && document.getElementById(m[1])) ||
                    (el.id && document.querySelector('[aria-errormessage="' + CSS.escape(el.id) + '"], [aria-describedby~="' + CSS.escape(el.id) + '"]'));
      if (owner) { const q = questionOf(owner); if (q) return q; }
    } catch (e) { /* fall through */ }
    // a control's own label by aria-labelledby or <label for> before any walk: the walk's first label can be another
    // field's (Ivalua, 2026-10-22: a required sponsorship-details textarea was named 'LinkedIn Profile') or a picked
    // chip's 'select__multi-value__label' (Twitch, 2026-10-22: the pick was cut from its own box and it read empty)
    try {
      const byIds = (el.getAttribute && el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)
        .map(i => { const n = document.getElementById(i); return n ? txt(n) : ''; }).filter(Boolean).join(' ').trim();
      if (byIds) return byIds;
      if (el.labels && el.labels.length) { const t = txt(el.labels[0]); if (t) return t; }
    } catch (e) { /* fall through */ }
    let p = el, hops = 0;
    // a design-system label shows its words through a <slot> (SmartRecruiters' screening boxes read '*', 2026-10-21)
    const slotText = (n) => { const ps = []; const w = (x, d) => { if (!x || d > 30) return; if (x.nodeType === 3) { ps.push(x.nodeValue || ''); return; }
      if (x.nodeType !== 1) return; if (x.tagName === 'SLOT') { for (const a of x.assignedNodes({ flatten: true })) w(a, d + 1); return; }
      for (const c of x.childNodes) w(c, d + 1); }; w(n, 0); return ps.join(' ').replace(/\s+/g, ' ').trim(); };
    while (p && hops < 6) { const l = p.querySelector ? p.querySelector('label, [class*="label" i]') : null;
      if (l) { const t = (l.querySelector && l.querySelector('slot')) ? slotText(l) : txt(l); if (t && t.replace(/[\s*:]+/g, '') && t.length < 120) return t.slice(0, 120); }
      p = p.parentElement; hops++; }
    const al = el.getAttribute && (el.getAttribute('aria-label') || ''); return (al || '').slice(0, 120);
  }
  function push(arr, obj, key) { if (seenText.has(key)) return; seenText.add(key); arr.push(obj); }
  // The posting's own text (Greenhouse's new boards put the description and the form on one page): an
  // eligibility statement there ("you must be a U.S. citizen") is not a form error.
  const DESC = '.job__description, [class*="job-description" i], [class*="jobdescription" i], [class*="job_description" i], ' +
               '[data-automation-id="jobPostingDescription"], [class*="posting-description" i], [class*="description__text" i]';
  function inDescription(el) { try { return !!(el.closest && el.closest(DESC)); } catch (e) { return false; } }
  // Text with no error markup counts only when it sits in a field's own wrapper or is styled red.
  function reddish(el) {
    try { const m = (el.ownerDocument.defaultView.getComputedStyle(el).color || '').match(/\d+(\.\d+)?/g); if (!m) return false;
          const r = +m[0], g = +m[1], b = +m[2]; return r >= 150 && r > g * 1.6 && r > b * 1.6; } catch (e) { return false; }
  }
  function nearField(el) {
    let p = el.parentElement;
    for (let i = 0; i < 2 && p; i++, p = p.parentElement) {
      try { if (p.querySelector('input:not([type="hidden"]), select, textarea, [role="combobox"], [role="listbox"], [role="radiogroup"]')) return true; } catch (e) {}
    }
    return false;
  }
  // "Please choose the closest conversion" under a GPA picker that already shows "3.7 - 4.0" is a hint, not an
  // error (Verkada on Greenhouse, 2026-10-18): unmarked please-select text next to a FILLED control is skipped.
  function hintBesideFilledField(el, text) {
    if (!/please\s+(select|choose|pick|enter|type)|press enter to confirm/i.test(text || '')) return false;
    let p = el.parentElement;
    for (let i = 0; i < 2 && p; i++, p = p.parentElement) {
      let ctls = []; try { ctls = Array.from(p.querySelectorAll('input:not([type="hidden"]), select, textarea, [role="combobox"]')); } catch (e) { ctls = []; }
      if (!ctls.length) continue;
      // decide on the nearest wrapper that holds a control; a wider ancestor would see other fields' values
      for (const c of ctls) {
        if (c.tagName === 'SELECT') { if (c.value && c.selectedOptions.length && txt(c.selectedOptions[0])) return true; continue; }
        if (c.type === 'checkbox' || c.type === 'radio') { if (c.checked) return true; continue; }
        if (String(c.value || '').trim()) return true;
        if (/cx-select-input/.test(String(c.className || ''))) continue;
        const box = c.closest && c.closest('[class*="__control"], [class*="control"], [class*="Control"], [class*="__value-container"]');
        const shown = box ? txt(box) : '';
        if (shown && !/^(select one|select\.\.\.|select|choose one|choose|search)$/i.test(shown)) return true;
      }
      return false;
    }
    return false;
  }
  function staleRequiredForSatisfiedChoiceGroup(el, text) {
    if (!/\b(required|choose|select)\b/i.test(text || '')) return false;
    let p = el;
    for (let hops = 0; p && hops < 7; hops++, p = p.parentElement) {
      let choices = []; try { choices = Array.from(p.querySelectorAll('input[type="checkbox"], input[type="radio"]')); } catch (e) { choices = []; }
      if (choices.length && choices.some(x => x.checked)) return true;
    }
    return false;
  }
  function walk(root) {
    if (!root || seenRoots.has(root)) return; seenRoots.add(root);
    const SEL = '[role="alert"], [aria-invalid="true"], [aria-errormessage], [class*="error" i], [class*="invalid" i], [data-automation-id*="error" i], [id*="error" i], .field-error, .form-error, .help-block';
    let marked = []; try { marked = root.querySelectorAll(SEL); } catch (e) { marked = []; }
    for (const el of marked) {
      if (!vis(el) || inDescription(el)) continue;
      const t = txt(el);
      if (!t || t.length > 400 || SKIP.test(t)) continue;
      const how = el.getAttribute('role') === 'alert' ? 'role=alert' : el.getAttribute('aria-invalid') === 'true' ? 'aria-invalid' : 'error-class';
      if (how !== 'aria-invalid') { if (STATUSY.test(t)) continue; if (!ERR.test(t)) continue; }
      if (staleRequiredForSatisfiedChoiceGroup(el, t)) continue;
      push(alerts, { text: t.slice(0, 300), how: how, near: nearestLabel(el) }, 'a:' + t);
    }
    let all = []; try { all = root.querySelectorAll('div,span,p,li,small,strong,label,td,th,legend,h1,h2,h3,h4,h5,h6'); } catch (e) { all = []; }
    for (const el of all) {
      if (el.children.length > 2) continue;
      if (!vis(el)) continue;
      if (el.tagName === 'LABEL' || (el.closest && el.closest('label'))) continue;
      // a block that WRAPS a field (label + control) is the question, not a message about it: CoVar's
      // "Are you a US citizen? ... clearance is required ..." plus its picked value was read as an error (2026-10-18)
      if (el.querySelector && el.querySelector('label, input, select, textarea, button, [role="combobox"]')) continue;
      if (el.closest && el.closest('.apply-flow-block__header')) continue;
      const t = txt(el);
      if (!t || t.length > 300 || SKIP.test(t)) continue;
      const promptKey = s => String(s || '').toLowerCase().replace(/[\s.:;!?]+$/g, '').trim();
      const isFieldPrompt = Array.from(document.querySelectorAll('label, legend')).some(l => promptKey(txt(l)) === promptKey(t));
      if (isFieldPrompt) continue;
      // A form-level summary after a refused submit ("Your form needs corrections / Missing entry for required field: X",
      // Ashby, Niantic 2026-10-18) is reported apart: it proves the last click was refused, but Ashby keeps it on screen
      // until the next submit, so it must not block the corrected resubmit.
      if (FORM_SUMMARY.test(t)) { push(summaries, { text: t.slice(0, 300), tag: el.tagName.toLowerCase() }, 's:' + t); continue; }
      if (!ERR.test(t)) continue;
      if (INSTRUCTIONAL.test(t)) continue;
      if (inDescription(el) || (!reddish(el) && !nearField(el))) continue;
      if (staleRequiredForSatisfiedChoiceGroup(el, t)) continue;
      if (!reddish(el) && hintBesideFilledField(el, t)) continue;
      push(errorText, { text: t.slice(0, 300), tag: el.tagName.toLowerCase(), near: nearestLabel(el) }, 'e:' + t);
    }
    let ctl = []; try { ctl = root.querySelectorAll('input, textarea, select'); } catch (e) { ctl = []; }
    for (const el of ctl) {
      // a disabled control cannot be filled: Greenhouse keeps 'End date year*' disabled for a current role (FanDuel, 2026-10-19)
      if (el.type === 'hidden' || !vis(el) || el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
      const req = el.required || el.getAttribute('aria-required') === 'true' || starRequired(el);
      if (!req) continue;
      // Paylocity marks every box of a work / education history address required, yet its Next Step takes the entry with
      // the address empty (Transcard, 2026-10-22); his history holds no employer street, county or ZIP to give
      if (/^public-candidate-[a-z-]*history-address-\d+-(address-1|address-2|city|county|zip|postal-code|us-state|state|province|country)$/.test(el.id || '')) continue;
      let empty;
      if (el.type === 'checkbox' || el.type === 'radio') {
        const peers = el.name ? Array.from(root.querySelectorAll('input[type="' + el.type + '"][name="' + CSS.escape(el.name) + '"]')) : [el];
        empty = !peers.some(x => x.checked);
      } else if (el.type === 'file') empty = !(el.files && el.files.length);
      else if (el.tagName === 'SELECT') empty = !el.value || !el.selectedOptions.length;
      else {
        empty = !String(el.value || '').trim();
        if (empty) {
          // a pick kept on the field's custom-element host (SmartRecruiters' City)
          let n = el;
          for (let i = 0; i < 3 && empty; i++) {
            const rn = n && n.getRootNode ? n.getRootNode() : null; const h = rn && rn.host ? rn.host : null; if (!h) break;
            let v = h.value;
            // a multiselect keeps the picked option ids as an array (SmartRecruiters' spl-multiselect-autocomplete: AbbVie's
            // major and locations read empty with 'Computer Science' and 'Open to all locations' picked, 2026-10-24)
            if (Array.isArray(v) && v.length) { empty = false; break; }
            if (v && typeof v === 'object') v = v.displayString || v.text || v.name || v.label || '';
            if (typeof v === 'string' && v.trim()) empty = false;
            n = h;
          }
        }
        if (empty) {
          const tl = el.closest('spl-tags-list');
          if (tl && tl.shadowRoot && tl.shadowRoot.querySelector('spl-tag')) empty = false;
        }
        if (empty) {
          const ms = el.closest('[data-automation-id="multiSelectContainer"]');
          if (ms && ms.querySelector('[data-automation-id="selectedItem"]')) empty = false;
        }
        if (empty) {
          const isCombo = el.getAttribute('role') === 'combobox' || el.getAttribute('aria-autocomplete') || el.hasAttribute('aria-expanded') || /react-select|__input/i.test(el.id || el.className || '');
          if (isCombo && !/cx-select-input/.test(String(el.className || ''))) {
            let c = el.closest('[class*="__control"], [class*="control"], [class*="Control"], [class*="__value-container"]');
            if (!c || !txt(c)) { c = null; let p = el.parentElement; for (let i = 0; i < 5 && p; i++, p = p.parentElement) { const t = txt(p); if (t && t.length < 240) { c = p; break; } } }
            let shown = c ? txt(c) : '';
            if (c && shown) { for (const l of c.querySelectorAll('label, legend')) { const lt = txt(l); if (lt) shown = shown.replace(lt, ''); } }
            if (c && shown) { for (const s of c.querySelectorAll('[role="status"], [aria-live], .sr-only, .visually-hidden')) { const st = txt(s); if (st) shown = shown.replace(st, ''); } }
            const own = nearestLabel(el) || '';
            if (shown && own && shown.trim().startsWith(own)) shown = shown.trim().slice(own.length);
            shown = shown.replace(/^[\s*:]+|[\s*:]+$/g, '').trim();
            if (shown && !/^(select one|select\.\.\.|select|choose one|choose|search|no selection)$/i.test(shown)) empty = false;
          }
        }
      }
      if (!empty) continue;
      // the question a control names by aria-labelledby: Qualcomm's Eightfold dropdowns sit six boxes below their label
      // and were reported as 'input-19', 'input-22', ... (2026-10-21)
      const rn = el.getRootNode ? el.getRootNode() : document;
      const byIds = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)
        .map(i => { const n = (rn && rn.getElementById ? rn.getElementById(i) : null) || document.getElementById(i); return n ? txt(n) : ''; })
        .filter(Boolean).join(' ');
      // ... or the starred label nine boxes up (Epic Games' react-selects were reported as 'react-select-14-input', 2026-10-23)
      const bh = boxHead(el);
      const lab = byIds || nearestLabel(el) || (bh ? txt(bh) : '') || el.getAttribute('aria-label') || el.name || el.id || '(unlabelled)';
      push(emptyRequired, { label: lab, type: el.type || el.tagName.toLowerCase(), name: el.name || el.id || '', ref: el.getAttribute('data-goat-ref') || '' }, 'r:' + lab + ':' + (el.name || el.id || ''));
    }
    // a required ARIA radio group with nothing checked (SAP SuccessFactors' radioGroup: the question label carries the '*')
    let rgs = []; try { rgs = root.querySelectorAll('[role="radiogroup"]'); } catch (e) { rgs = []; }
    for (const g of rgs) {
      if (!vis(g) || g.querySelector('input[type="radio"]')) continue;
      const lb = g.getAttribute('aria-labelledby') || '';
      const gr = g.getRootNode ? g.getRootNode() : null;
      const qn = lb ? ((gr && gr.getElementById ? gr.getElementById(lb.split(/\s+/)[0]) : null) || g.ownerDocument.getElementById(lb.split(/\s+/)[0]) || null) : null;
      // the question may sit in a slot (SmartRecruiters' screening radios, CapTech 2026-10-21)
      const slotted = (n) => { const ps = []; const w = (x, d) => { if (!x || d > 30) return; if (x.nodeType === 3) { ps.push(x.nodeValue || ''); return; }
        if (x.nodeType !== 1) return; if (x.tagName === 'SLOT') { for (const a of x.assignedNodes({ flatten: true })) w(a, d + 1); return; }
        for (const c of x.childNodes) w(c, d + 1); }; w(n, 0); return ps.join(' ').replace(/\s+/g, ' ').trim(); };
      const q = qn ? (qn.querySelector('slot') ? slotted(qn) : txt(qn)) : (g.getAttribute('aria-label') || '');
      const req = g.getAttribute('aria-required') === 'true' || /^\s*\*|\*\s*$/.test(q) || !!(qn && qn.querySelector('.requiredField'));
      if (!req) continue;
      let opts = Array.from(g.querySelectorAll('[role="radio"]'));
      if (!opts.length) for (const sl of g.querySelectorAll('slot')) opts.push(...sl.assignedElements({ flatten: true }).filter(o => o.getAttribute('role') === 'radio'));
      if (!opts.length || opts.some(o => o.getAttribute('aria-checked') === 'true')) continue;
      push(emptyRequired, { label: q.replace(/^\s*\*\s*/, '').slice(0, 160) || '(radio group)', type: 'radiogroup', name: g.id || '', ref: opts[0].getAttribute('data-goat-ref') || '' }, 'r:' + q + ':radiogroup');
    }
    // a required Yes / No pair of toggle buttons with neither pressed (Ashby: Walrus' 'Are you authorized to work in the
    // United States?' lost its Yes before the submit and the gate passed, 2026-10-23)
    let yns = []; try { yns = root.querySelectorAll('.ashby-application-form-input-yesno, [class*="_yesno_"]'); } catch (e) { yns = []; }
    for (const g of yns) {
      if (!vis(g)) continue;
      const btns = Array.from(g.querySelectorAll('button[aria-pressed], [role="button"][aria-pressed]'));
      if (btns.length < 2 || btns.some(b => b.getAttribute('aria-pressed') === 'true')) continue;
      const entry = g.closest('.ashby-application-form-field-entry, [class*="fieldEntry"], fieldset') || g.parentElement;
      const lab = entry ? entry.querySelector('label, legend') : null;
      const q = lab ? txt(lab) : '';
      const req = g.getAttribute('aria-required') === 'true' || !!(lab && (/required/i.test(String(lab.className || '')) || /^\s*\*|\*\s*$/.test(q)));
      if (!req) continue;
      push(emptyRequired, { label: q.replace(/^\s*\*\s*|\s*\*\s*$/g, '').slice(0, 160) || '(yes / no)', type: 'yesno', name: '', ref: btns[0].getAttribute('data-goat-ref') || '' }, 'r:' + q + ':yesno');
    }
    let dd = []; try { dd = root.querySelectorAll('[data-automation-id="selectWidget"], button[aria-haspopup="listbox"], [role="combobox"]:not(input):not(textarea), [role="button"][aria-haspopup]'); } catch (e) { dd = []; }
    for (const el of dd) {
      if (!vis(el)) continue;
      const aria = el.getAttribute('aria-label') || '';
      const req = /required/i.test(aria) || el.getAttribute('aria-required') === 'true';
      if (!req) continue;
      const shown = txt(el).slice(0, 120);
      if (!/^(select one|select\.\.\.|select|choose one|choose|search|)$/i.test(shown)) continue;
      const lab = (nearestLabel(el) || aria.replace(/\s*(Select One|Required|selected)\s*/gi, ' ').trim() || '(dropdown)');
      push(emptyRequired, { label: lab, type: 'dropdown', name: el.getAttribute('data-automation-id') || el.id || '', ref: el.getAttribute('data-goat-ref') || '' }, 'r:' + lab + ':dropdown');
    }
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) walk(el.shadowRoot);
      if (el.tagName === 'IFRAME') { try { if (el.contentDocument) walk(el.contentDocument); } catch (e) {} }
    }
  }
  walk(document);
  // SAP SuccessFactors folds its form into sections (a header button with aria-expanded): a folded section hides its
  // questions from every check, and a re-opened application came back with all of them folded (CenturyLink, 2026-10-21).
  const folded = [];
  try {
    if (document.querySelector('a[id$="_expandAllSections"]'))
      for (const b of document.querySelectorAll('button[id$=":topBar"][aria-expanded="false"]'))
        if (vis(b)) folded.push(txt(b).slice(0, 60) || '(section)');
  } catch (e) {}
  return JSON.stringify({ url: location.href, alerts: alerts.slice(0, 40), error_text: errorText.slice(0, 40), empty_required: emptyRequired.slice(0, 40), summaries: summaries.slice(0, 10), collapsed_sections: folded.slice(0, 12) });
}
"""

JS_SUBMIT_SIGNAL = r"""
() => {
  const CONFIRM = /(application\s+(submitted|received|complete|completed)|thank\s+you\s+for\s+(applying|your\s+(application|interest|submission))|(we|we've|we have)\s+received\s+your\s+application|your\s+application\s+has\s+been\s+(submitted|received|sent)|submission\s+(successful|received|complete)|successfully\s+(submitted|applied)|application\s+id\b|confirmation\s+number)/i;
  const t = (document.body ? (document.body.innerText || '') : '').replace(/\s+/g, ' ');
  const m = t.match(CONFIRM);
  const forms = document.querySelectorAll('form').length;
  const btns = Array.from(document.querySelectorAll('button, input[type=submit], [role=button]'))
    .filter(b => /^(submit|submit application|apply|send application|finish|complete)/i.test(((b.innerText || b.value || '') + '').replace(/\s+/g, ' ').trim())).length;
  return JSON.stringify({ url: location.href, confirm_match: m ? m[0].slice(0, 160) : '', forms: forms, submit_buttons: btns, text_head: t.slice(0, 400) });
}
"""

JS_FIND_SUBMIT = r"""
(label) => {
""" + _COMMON + r"""
  const want = (label || '').trim().toLowerCase();
  const DEF = /^(submit|submit application|apply|apply now|send application|finish|complete|complete application|submit my application)$/i;
  const cands = [];
  for (const root of allRoots(document)) { try { cands.push(...root.querySelectorAll('button, input[type=submit], input[type=button], [role=button], a[href="#"]')); } catch (e) {} }
  for (const b of cands) {
    const own = ((b.innerText || b.value || b.getAttribute('aria-label') || '') + '').replace(/\s+/g, ' ').trim();
    // SmartRecruiters' final button is a bare <button> in an spl-button's shadow root: its host carries the name
    const t = (own ? [own] : shadowHostNames(b)).find(n => want ? n.toLowerCase() === want : DEF.test(n)) || '';
    if (!t) continue;
    const r = b.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;
    if (b.disabled || b.getAttribute('aria-disabled') === 'true') continue;
    // never a button inside an extension panel / outside the page's own form area is not knowable here;
    // the Python side additionally checks the element at the click point.
    b.scrollIntoView({ block: 'center' });
    const r2 = b.getBoundingClientRect();
    return JSON.stringify({ ok: true, text: t, x: Math.round(r2.x + r2.width / 2), y: Math.round(r2.y + r2.height / 2), w: Math.round(r2.width), h: Math.round(r2.height) });
  }
  return JSON.stringify({ ok: false, tried: cands.length });
}
"""

# --------------------------------------------------------------------------- challenge detection
JS_CHALLENGE = r"""
() => {
  const vis = (el) => { try { const r = el.getBoundingClientRect(); const s = getComputedStyle(el); return r.width > 20 && r.height > 20 && s.visibility !== 'hidden' && s.display !== 'none'; } catch (e) { return false; } };
  const frames = [];
  for (const f of document.querySelectorAll('iframe')) {
    const src = f.getAttribute('src') || '';
    const title = f.getAttribute('title') || '';
    let kind = '';
    if (/recaptcha\/api2\/anchor|recaptcha\/enterprise\/anchor/i.test(src)) kind = 'recaptcha_checkbox';
    else if (/recaptcha\/api2\/bframe|recaptcha\/enterprise\/bframe/i.test(src)) kind = 'recaptcha_challenge';
    else if (/hcaptcha\.com/i.test(src)) kind = 'hcaptcha';
    else if (/challenges\.cloudflare\.com|turnstile/i.test(src)) kind = 'turnstile';
    else if (/arkoselabs|funcaptcha/i.test(src)) kind = 'puzzle';
    else if (/captcha/i.test(src + ' ' + title)) kind = 'captcha';
    if (!kind) continue;
    const r = f.getBoundingClientRect();
    // Greenhouse, Lever and Ashby carry an INVISIBLE reCAPTCHA whose anchor iframe (256x60) sits inside
    // the fixed .grecaptcha-badge container. That badge is never a challenge; only the 304x78
    // "I'm not a robot" widget or a bframe/hcaptcha/turnstile challenge is.
    const inBadge = !!f.closest('.grecaptcha-badge') || (kind === 'recaptcha_checkbox' && r.width <= 260 && r.height <= 70);
    if (inBadge) kind = 'recaptcha_badge';
    frames.push({ kind, visible: vis(f), x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) });
  }
  // The invisible reCAPTCHA badge is NOT a challenge.
  const badgeOnly = frames.length > 0 && frames.every(f => !f.visible || f.kind === 'recaptcha_badge');
  const body = (document.body ? document.body.innerText : '').replace(/\s+/g, ' ');
  // TikTok's ByteDance puzzle (2026-10-21) is drawn in a page container, not an iframe: a visible captcha box of real size,
  // or its own instructions, is a challenge too
  const domCaptcha = Array.from(document.querySelectorAll('[id*="captcha" i], [class*="captcha" i]')).some(e => {
    if (e.closest('.grecaptcha-badge')) return false;
    const r = e.getBoundingClientRect(); return vis(e) && r.width >= 150 && r.height >= 100; });
  if (domCaptcha) frames.push({ kind: 'captcha', visible: true, x: 0, y: 0, w: 0, h: 0, dom: true });
  const textChallenge = /(i'?m not a robot|verify (that )?you are (a )?human|complete the (security )?check|select all images|prove you'?re not a robot|drag the (slider|puzzle)|slide to (verify|complete)|select (2|two) objects that are the same shape|rotate the (image|picture)|complete the puzzle)/i.test(body);
  const codePage = /(verification|security|confirmation|one[- ]time) code/i.test(body) && /(sent|emailed|e-mailed) (to|you)/i.test(body);
  const codeInputs = Array.from(document.querySelectorAll('input')).filter(i => vis(i) && (/security-input|otp|verification|code/i.test((i.id || '') + ' ' + (i.name || '') + ' ' + (i.getAttribute('autocomplete') || '')) || i.maxLength === 1 && (i.inputMode === 'numeric' || i.type === 'tel' || i.type === 'text')));
  const visibleChallenge = frames.some(f => f.visible && f.kind !== 'recaptcha_badge' && f.kind !== 'recaptcha_checkbox') || frames.some(f => f.visible && f.kind === 'recaptcha_checkbox' && f.w >= 290) || (textChallenge && !codePage);
  return JSON.stringify({ challenge: !!visibleChallenge, kinds: Array.from(new Set(frames.filter(f => f.visible && f.kind !== 'recaptcha_badge').map(f => f.kind))), badge_only: badgeOnly, frames,
    email_code: !!(codePage && codeInputs.length), code_inputs: codeInputs.length, code_input_refs: codeInputs.map(i => i.getAttribute('data-goat-ref') || '').filter(Boolean) });
}
"""

# --------------------------------------------------------------------------- fill
JS_FILL_BATCH = r"""
(fields) => {
""" + _COMMON + r"""
  const out = [];
  const setNative = (el, value) => {
    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
    el.focus();
    try { el.select && el.select(); } catch (e) {}
    if (desc && desc.set) desc.set.call(el, ''); else el.value = '';
    el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'deleteContentBackward' }));
    if (desc && desc.set) desc.set.call(el, value); else el.value = value;
    el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: value }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    el.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true }));
    el.blur();
    el.dispatchEvent(new FocusEvent('blur', { bubbles: true }));
    el.dispatchEvent(new FocusEvent('focusout', { bubbles: true }));
  };
  for (const f of fields) {
    const res = { ref: f.ref, ok: false, readback: '', error: '' };
    const el = findRef(f.ref);
    if (!el) { res.error = 'ref not found'; out.push(res); continue; }
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    try {
      if (tag === 'input' && (type === 'checkbox' || type === 'radio')) {
        const want = (f.value === true || /^(true|yes|on|checked|1)$/i.test(String(f.value)));
        if (type === 'radio') {
          // a radio cannot be unticked: a false value is refused instead of clicking the option anyway
          if (!want) { res.error = 'a radio option cannot be unticked: tick the option that matches the answer instead'; res.readback = el.checked ? 'checked' : 'unchecked'; out.push(res); continue; }
          if (!el.checked) el.click();
        }
        else if (el.checked !== want) el.click();
        res.readback = el.checked ? 'checked' : 'unchecked'; res.ok = (type === 'radio') ? el.checked : (el.checked === want);
      } else if (tag === 'select') {
        const want = String(f.value).trim().toLowerCase();
        let opt = Array.from(el.options).find(o => txt(o).toLowerCase() === want || (o.value || '').toLowerCase() === want);
        if (!opt) opt = Array.from(el.options).find(o => txt(o).toLowerCase().startsWith(want));
        if (!opt) opt = Array.from(el.options).find(o => txt(o).toLowerCase().includes(want));
        if (!opt) { res.error = 'no option matches; options: ' + Array.from(el.options).map(o => txt(o)).slice(0, 15).join(' | '); out.push(res); continue; }
        el.focus(); el.value = opt.value;
        el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); el.blur();
        res.readback = txt(el.selectedOptions[0] || {}); res.ok = el.selectedOptions.length > 0 && el.selectedOptions[0] === opt;
      } else if ((tag === 'button' || /^(radio|checkbox|switch)$/.test(el.getAttribute('role') || '')) && (el.hasAttribute('aria-pressed') || el.hasAttribute('aria-checked'))) {
        const want = (f.value === true || /^(true|yes|on|checked|1|pressed)$/i.test(String(f.value)));
        const isOn = () => (el.getAttribute('aria-pressed') ?? el.getAttribute('aria-checked')) === 'true';
        // A toggle that has to change is pressed by the caller with a REAL mouse click: a script click turned Ashby's
        // Yes/No button on screen while its form still reported the answer missing (Niantic, ibotta, 2026-10-18).
        if (isOn() !== want) { res.real_click = true; res.want = want; el.scrollIntoView({ block: 'center', inline: 'nearest' }); }
        res.readback = isOn() ? 'pressed' : 'unpressed'; res.ok = isOn() === want;
      } else if (tag === 'input' && type === 'file') {
        res.error = 'use upload_resume'; out.push(res); continue;
      } else if (tag === 'input' || tag === 'textarea') {
        if (type === 'password') { res.error = 'password fields are filled by the credentials helper'; out.push(res); continue; }
        if (el.getAttribute('data-uxi-widget-type') === 'selectinput' || el.closest('[data-automation-id="multiSelectContainer"]')) {
          // typed text never commits in a Workday prompt; Workday drops it on blur (Aerospace Corporation, 2026-10-19)
          res.error = 'this is a Workday prompt (search, then pick from its list): use choose with the answer_key'; out.push(res); continue;
        }
        if (/cx-select-input/.test(String(el.className || ''))) {
          // an Oracle picker keeps only a value picked from its list (American Express' ZIP Code, 2026-10-19)
          res.error = 'picker: an Oracle list picker; set it with choose'; out.push(res); continue;
        }
        let v = String(f.value);
        if (type === 'number' && !/^-?\d+(\.\d+)?$/.test(v.trim())) {
          // A number box for a month ("MM", 1-12) or a year ("YYYY") takes that part of a month-year answer: Epic Games'
          // Start Date (Month) is <input type=number placeholder=MM> and "August 2025" left it empty (2026-10-19).
          const MONTHS = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'];
          const ph = (el.getAttribute('placeholder') || '').trim().toUpperCase();
          const mn = Number(el.getAttribute('min') || 0), mx = Number(el.getAttribute('max') || 0);
          const yr = (v.match(/\b(19|20)\d{2}\b/) || [])[0];
          const mi = MONTHS.findIndex(m => new RegExp('\\b' + m, 'i').test(v));
          const mm = v.match(/^\s*(\d{1,2})[\/-]/);
          if ((ph === 'MM' || (mn === 1 && mx === 12)) && (mi >= 0 || mm)) v = String(mi >= 0 ? mi + 1 : Number(mm[1]));
          else if ((ph === 'YYYY' || mn >= 1900) && yr) v = yr;
        }
        setNative(el, v);
        res.readback = (el.value || '').slice(0, 300); res.ok = (el.value || '') === v || (type === 'number' && Number(el.value) === Number(v));
        if (!res.ok && (type === 'tel' || /phone/i.test((el.name || '') + ' ' + (el.id || '')))) {
          // the page formats a phone number its own way: "(281) 832-1205" is "281-832-1205" (Epic Games, 2026-10-19)
          const dv = (el.value || '').replace(/\D/g, ''), dw = v.replace(/\D/g, '');
          if (dv && dw && (dv === dw || dv.endsWith(dw) || dw.endsWith(dv))) res.ok = true;
        }
        const kept = (el.value || '').length, sent = String(f.value).length;
        if (!res.ok && kept > 0 && kept < sent && String(f.value).startsWith(el.value || '')) {
          res.error = 'the box kept only ' + kept + ' of ' + sent + ' characters (a length cap); shorten the text to fit and fill it again';
        }
        if (!res.ok && !res.error && (el.getAttribute('role') === 'combobox' || el.getAttribute('aria-autocomplete')
            || /react-select|select__input/i.test((el.id || '') + ' ' + (typeof el.className === 'string' ? el.className : '')))) {
          res.error = 'this box is a dropdown that keeps only a picked option: use choose with the answer_key (a month-year answer also offers its month name and year)';
        }
      } else if (el.getAttribute('contenteditable') === 'true') {
        el.focus(); document.execCommand('selectAll', false, null); document.execCommand('insertText', false, String(f.value));
        res.readback = txt(el).slice(0, 300); res.ok = res.readback.replace(/\s+/g, ' ') === String(f.value).replace(/\s+/g, ' ').trim();
      } else {
        res.error = 'not a fillable control (' + tag + '); use choose or click';
      }
    } catch (e) { res.error = String(e).slice(0, 200); }
    out.push(res);
  }
  return JSON.stringify(out);
}
"""

JS_READ_REF = r"""
(ref) => {
""" + _COMMON + r"""
  const el = findRef(ref);
  if (!el) return JSON.stringify({ found: false });
  const tag = el.tagName.toLowerCase(); const type = (el.getAttribute('type') || '').toLowerCase();
  const r = el.getBoundingClientRect();
  let value = '';
  if (tag === 'select') value = el.selectedOptions.length ? txt(el.selectedOptions[0]) : '';
  else if (type === 'password') value = el.value ? '(set)' : '';
  else if (type === 'file') value = el.files && el.files.length ? el.files[0].name : '';
  else if (type === 'checkbox' || type === 'radio') value = el.checked ? 'checked' : 'unchecked';
  // A dropdown button shows its pick as text; its value attribute is an internal id on Workday (Booz Allen, 2026-10-18).
  // ... but an Oracle cx-select input holds its pick in its own value (Dell's ZIP / City read '' as a button, 2026-10-24)
  else if (tag === 'input' && /cx-select-input/.test(String(el.className || ''))) value = (el.value || '').slice(0, 300);
  else if (tag === 'button' || el.getAttribute('aria-haspopup') || el.getAttribute('role') === 'combobox' && tag !== 'input') value = txt(el).slice(0, 300);
  else if ('value' in el) value = (el.value || '').slice(0, 300);
  else value = txt(el).slice(0, 300);
  let shown = '';
  const c = /cx-select-input/.test(String(el.className || '')) ? null : el.closest('[class*="__control"], [class*="control"], [class*="Control"], [data-automation-id^="formField"], .field, .form-group');
  if (c) {
    shown = txt(c);
    for (const s of c.querySelectorAll('[role="status"], [aria-live], .sr-only, .visually-hidden')) { const st = txt(s); if (st) shown = shown.replace(st, ''); }
    shown = shown.replace(/\s+/g, ' ').trim().slice(0, 300);
  }
  if (!value && tag === 'input') { const hv = hostPickedValue(el); if (hv) shown = hv.slice(0, 300); }
  el.scrollIntoView({ block: 'center', inline: 'nearest' });
  let r2 = el.getBoundingClientRect();
  // A react-select search input can be a few pixels wide (Greenhouse job boards: 3.5 px): a click there
  // misses the widget, so aim at its control box instead.
  if (r2.width < 8 && (el.getAttribute('role') === 'combobox' || /__input|select__input/i.test(el.className || ''))) {
    const ctl = el.closest('[class*="__control"], [class*="control"], [class*="Control"]');
    if (ctl) { const rc = ctl.getBoundingClientRect(); if (rc.width > 20 && rc.height > 10) r2 = rc; }
  }
  return JSON.stringify({ found: true, tag, type, label: labelFor(el), value, shown, x: Math.round(r2.x + r2.width / 2), y: Math.round(r2.y + r2.height / 2), w: Math.round(r2.width), h: Math.round(r2.height),
    disabled: !!el.disabled, expanded: el.getAttribute('aria-expanded'), invalid: el.getAttribute('aria-invalid') === 'true',
    combo: el.getAttribute('role') === 'combobox' || !!el.getAttribute('aria-autocomplete') || !!el.getAttribute('aria-haspopup'),
    pressed: (el.hasAttribute('aria-pressed') || el.hasAttribute('aria-checked')) ? (el.getAttribute('aria-pressed') ?? el.getAttribute('aria-checked')) === 'true' : null });
}
"""

# Current values of several refs at once, for the read-back diff. Unlike JS_READ_REF it never scrolls:
# it only reports {ref: {value|shown|checked|pressed}} for the refs it finds.
JS_READ_REFS = r"""
(refs) => {
""" + _COMMON + r"""
  const out = {};
  for (const ref of refs || []) {
    const el = findRef(ref);
    if (!el) continue;
    const tag = el.tagName.toLowerCase(); const type = (el.getAttribute('type') || '').toLowerCase();
    if (tag === 'select') out[ref] = { value: el.selectedOptions.length ? txt(el.selectedOptions[0]) : '' };
    else if (type === 'password' || type === 'file') out[ref] = { value: el.value ? '(set)' : '' };
    else if (type === 'checkbox' || type === 'radio') out[ref] = { checked: !!el.checked };
    else if ((tag === 'button' || /^(radio|checkbox|switch)$/.test(el.getAttribute('role') || '')) && (el.hasAttribute('aria-pressed') || el.hasAttribute('aria-checked')))
      out[ref] = { pressed: (el.getAttribute('aria-pressed') ?? el.getAttribute('aria-checked')) === 'true' };
    else if (tag === 'input' && /cx-select-input/.test(String(el.className || ''))) out[ref] = { value: (el.value || '').slice(0, 300) };
    else if (tag === 'button' || el.getAttribute('aria-haspopup') || (el.getAttribute('role') === 'combobox' && tag !== 'input'))
      out[ref] = { value: txt(el).slice(0, 300) };
    else if ('value' in el) out[ref] = { value: (el.value || '').slice(0, 300) };
    else out[ref] = { value: txt(el).slice(0, 300) };
    const msBox = el.closest('[data-automation-id="multiSelectContainer"]');
    if (msBox) {
      const picked = Array.from(msBox.querySelectorAll('[data-automation-id="selectedItem"]')).map(x => txt(x)).filter(Boolean);
      if (picked.length) out[ref] = { value: picked.join('; ').slice(0, 300) };
    }
    const c = /cx-select-input/.test(String(el.className || '')) ? null : el.closest('[class*="__control"], [class*="control"], [class*="Control"]');
    if (c && tag === 'input' && out[ref].value === '') { const s = txt(c).slice(0, 300); if (s && !PLACEHOLDER.test(s)) out[ref].shown = s; }
    // a pick kept on the field's custom-element host while its box was emptied (SmartRecruiters' City, ServiceNow
    // 2026-10-21: the read-back before Next called it '' and blocked the page five times)
    if (tag === 'input' && out[ref].value === '' && !out[ref].shown) { const hv = hostPickedValue(el); if (hv) out[ref].shown = hv.slice(0, 300); }
  }
  return JSON.stringify(out);
}
"""

# The choices of a native control by ref: a <select>'s options, or the labels of a radio/checkbox group.
JS_FIELD_CHOICES = r"""
(ref) => {
""" + _COMMON + r"""
  const el = findRef(ref);
  if (!el) return JSON.stringify(null);
  const tag = el.tagName.toLowerCase(), type = (el.getAttribute('type') || '').toLowerCase();
  if (tag === 'select') return JSON.stringify(Array.from(el.options).map(o => txt(o)).filter(t => t && !/^(select|select one|choose|--)/i.test(t)));
  if ((type === 'radio' || type === 'checkbox') && el.name) {
    const peers = Array.from(document.querySelectorAll('input[type="' + type + '"][name="' + CSS.escape(el.name) + '"]'));
    return JSON.stringify(peers.map(x => labelFor(x)).filter(Boolean));
  }
  return JSON.stringify(null);
}
"""

JS_LIST_OPTIONS = r"""
() => {
  const vis = (el) => { const r = el.getBoundingClientRect(); return r.width > 1 && r.height > 1; };
  const txt = (el) => ((el.getAttribute && el.getAttribute('data-automation-label')) || el.innerText || el.textContent || '').replace(/[\uE000-\uF8FF]/g, '').replace(/\s+/g, ' ').trim();
  // Oracle Candidate Experience selects open a role=grid popup whose options are gridcells (Amex, 2026-10-19);
  // SAP SuccessFactors' paginated selects list role=menuitem links inside a role=menu (L3Harris, 2026-10-20)
  const opts = Array.from(document.querySelectorAll('[role="option"], [data-automation-id="promptOption"], [data-automation-id="menuItem"], li[role="presentation"] div[role="option"], .select__option, [class*="__option"], [class*="option" i][id*="option" i], ul[role="listbox"] li, [role="listbox"] [role="option"], [role="grid"] [role="gridcell"], [role="menu"] [role="menuitem"]')).filter(o => !o.closest('[data-automation-id="selectedItemList"]'));
  const out = [];
  for (const o of opts) {
    if (!vis(o)) continue;
    const r = o.getBoundingClientRect();
    const t = txt(o).slice(0, 160); if (!t) continue;
    // An option can be several lines, name first (Ashby's schools: "Texas A&M University / United States / tamu.edu").
    const first = ((o.innerText || '').split('\n').map(s => s.trim()).find(Boolean) || '').replace(/\s+/g, ' ').slice(0, 160);
    out.push({ text: t, first, x: Math.round(r.x + Math.min(30, r.width / 2)), y: Math.round(r.y + r.height / 2), selected: o.getAttribute('aria-selected') === 'true' });
  }
  return JSON.stringify(out.slice(0, 400));
}
"""

# Bring one option of an open list into view (long Workday lists scroll inside the popup) and return
# where to click it now. Prefers an option inside an open listbox over a chip of an already-chosen value.
JS_SCROLL_OPTION = r"""
(text) => {
  const txt = (el) => ((el.getAttribute && el.getAttribute('data-automation-label')) || el.innerText || el.textContent || '').replace(/[\uE000-\uF8FF]/g, '').replace(/\s+/g, ' ').trim();
  const opts = Array.from(document.querySelectorAll('[role="option"], [data-automation-id="promptOption"], [data-automation-id="menuItem"], .select__option, [class*="__option"], ul[role="listbox"] li, [role="grid"] [role="gridcell"], [role="menu"] [role="menuitem"]'))
    .filter(o => txt(o).slice(0, 160) === text && o.getBoundingClientRect().width > 1);
  if (!opts.length) return JSON.stringify({ ok: false });
  const inList = opts.filter(o => !o.closest('[data-automation-id="selectedItemList"]'));
  const el = (inList.length ? inList : opts)[inList.length ? inList.length - 1 : 0];
  el.scrollIntoView({ block: 'center', inline: 'nearest' });
  const r = el.getBoundingClientRect();
  return JSON.stringify({ ok: true, x: Math.round(r.x + Math.min(30, r.width / 2)), y: Math.round(r.y + r.height / 2) });
}
"""

# Did a picker really commit? Workday prompts show the typed search text in the input either way; only a
# selected-item chip (and the field's error going away) means the value was taken.
JS_FIELD_COMMIT = r"""
(ref) => {
""" + _COMMON + r"""
  const el = findRef(ref);
  if (!el) return JSON.stringify({ found: false });
  const box = el.closest('[data-automation-id^="formField"], [data-automation-id="multiSelectContainer"], .field, .form-group, [class*="field" i]') || el.parentElement;
  const scope = box || document;
  const chips = Array.from(scope.querySelectorAll('[data-automation-id="selectedItem"], [class*="multi-value__label" i], [class*="selected-item" i], [class*="chip" i]'))
    .filter(vis).map(c => txt(c)).filter(Boolean);
  // An expanded prompt (a category opened, Gordon Food Service 2026-10-20) makes the multiSelectContainer itself the
  // closest box: it matches the selector rather than containing it, and it is still a prompt.
  const PROMPT_SEL = '[data-automation-id="multiSelectContainer"], [data-uxi-widget-type="multiselect"], [data-automation-id="promptIcon"], [data-automation-id="promptSearchButton"]';
  const ff = el.closest('[data-automation-id^="formField"]');
  const prompt = !!((box && (box.matches(PROMPT_SEL) || box.querySelector(PROMPT_SEL))) || (ff && ff.querySelector(PROMPT_SEL))
                    || el.closest('[data-automation-id="multiSelectContainer"], [data-uxi-widget-type="multiselect"]'));
  const errEl = box ? Array.from(box.querySelectorAll('[data-automation-id="errorMessage"], [role="alert"], [class*="error" i]')).find(e => vis(e) && txt(e)) : null;
  return JSON.stringify({ found: true, prompt, chips: chips.slice(0, 12), value: (el.value || '').slice(0, 160),
                          invalid: el.getAttribute('aria-invalid') === 'true' || !!errEl, error: errEl ? txt(errEl).slice(0, 160) : '',
                          cx: /cx-select-input/.test(String(el.className || '')),
                          cxInvalid: /cx-select-input--invalid/.test(String(el.className || '')) || el.getAttribute('aria-invalid') === 'true' });
}
"""

# Select an open option the way a person would when its text is not the clickable part: tick the radio or
# checkbox inside it, else give the option element itself a full pointer/mouse/click sequence.
JS_COMMIT_OPTION = r"""
(text) => {
  const txt = (el) => ((el.getAttribute && el.getAttribute('data-automation-label')) || el.innerText || el.textContent || '').replace(/[\uE000-\uF8FF]/g, '').replace(/\s+/g, ' ').trim();
  const opts = Array.from(document.querySelectorAll('[role="option"], [data-automation-id="promptOption"], [data-automation-id="menuItem"], .select__option, [class*="__option"], ul[role="listbox"] li'))
    .filter(o => txt(o).slice(0, 160) === text && o.getBoundingClientRect().width > 1 && !o.closest('[data-automation-id="selectedItemList"]'));
  if (!opts.length) return JSON.stringify({ ok: false });
  const o = opts[opts.length - 1];
  const box = o.querySelector('input[type="radio"], input[type="checkbox"], [role="radio"], [role="checkbox"]');
  const target = box || o;
  for (const t of ['pointerdown', 'mousedown', 'pointerup', 'mouseup']) target.dispatchEvent(new MouseEvent(t, { bubbles: true, cancelable: true, view: window }));
  target.click();
  return JSON.stringify({ ok: true, via: box ? 'inner-control' : 'option' });
}
"""

JS_REACT_SELECT = r"""
([ref, wantRegexSrc]) => {
""" + _COMMON + r"""
  const el = findRef(ref); if (!el) return JSON.stringify({ ok: false, error: 'ref not found' });
  const ctrl = el.closest('[class*="select__control"], [class*="__control"], [class*="control"]') || el;
  const key = Object.keys(ctrl).find(k => k.startsWith('__reactFiber'));
  if (!key) return JSON.stringify({ ok: false, error: 'not a react component' });
  let f = ctrl[key];
  while (f && !(f.memoizedProps && f.memoizedProps.options && f.memoizedProps.selectOption)) f = f.return;
  if (!f) return JSON.stringify({ ok: false, error: 'select props not found' });
  const flat = []; const walk = (o) => { for (const x of o) { if (x && x.options) walk(x.options); else flat.push(x); } }; walk(f.memoizedProps.options);
  // null: list the option labels (the caller picks by its own preference order); "=label": exact label;
  // anything else: a regex source, first option in page order that matches.
  if (wantRegexSrc === null || wantRegexSrc === undefined)
    return JSON.stringify({ ok: false, list: true, options: flat.map(o => String(o.label)).slice(0, 3000) });
  let want;
  if (String(wantRegexSrc).startsWith('=')) { const t = String(wantRegexSrc).slice(1); want = flat.find(o => String(o.label) === t); }
  else { const re = new RegExp(wantRegexSrc, 'i'); want = flat.find(o => re.test(String(o.label))); }
  if (!want) return JSON.stringify({ ok: false, error: 'no option matched', options: flat.map(o => String(o.label)).slice(0, 40) });
  f.memoizedProps.selectOption(want);
  return JSON.stringify({ ok: true, selected: String(want.label), options: flat.length });
}
"""

# Workday renders a date as three spinbuttons (month / day / year) plus a Calendar button. Typing into
# the visible box does not commit the year: each section has to be filled on its own. Tag the sections
# so the Python side can click and type into them with real keys.
JS_DATE_SECTIONS = r"""
(ref) => {
""" + _COMMON + r"""
  let counter = 0;
  for (const root of allRoots(document)) for (const el of root.querySelectorAll('[data-goat-ref]')) {
    const n = parseInt((el.getAttribute('data-goat-ref') || 'e0').slice(1), 10); if (n > counter) counter = n;
  }
  const mkref = (el) => { let r = el.getAttribute('data-goat-ref'); if (!r) { r = 'e' + (++counter); el.setAttribute('data-goat-ref', r); } return r; };
  const start = ref ? findRef(ref) : null;
  if (!start) return JSON.stringify({ found: false, error: 'ref not found' });
  const box = start.closest('[data-automation-id^="formField"], [data-automation-id*="date" i], fieldset, .field, div') || start;
  const part = (el) => {
    const hay = ((el.getAttribute('data-automation-id') || '') + ' ' + (el.id || '') + ' ' + (el.getAttribute('aria-label') || '')).toLowerCase();
    if (/month/.test(hay)) return 'month';
    if (/\bday\b|date-?section-?day/.test(hay)) return 'day';
    if (/year/.test(hay)) return 'year';
    return '';
  };
  const out = [];
  let scope = box;
  for (let i = 0; i < 4 && scope; i++, scope = scope.parentElement) {
    const cands = Array.from(scope.querySelectorAll('input, [role="spinbutton"]')).filter(vis);
    for (const el of cands) {
      const p = part(el);
      if (p && !out.some(o => o.part === p)) {
        const r = el.getBoundingClientRect();
        out.push({ part: p, ref: mkref(el), value: (el.value || el.textContent || '').trim(),
                   x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) });
      }
    }
    if (out.length >= 2) break;
  }
  const boxText = (box.innerText || '').replace(/\s+/g, ' ').trim();
  // Workday writes "Invalid Date" in the field container, outside the small date wrapper.
  const field = start.closest('[data-automation-id^="formField"], fieldset, .field, .form-group') || box;
  const fieldText = (field.innerText || '').replace(/\s+/g, ' ').trim();
  const ariaBad = out.some(o => { const el = findRef(o.ref); return el && el.getAttribute('aria-invalid') === 'true'; });
  // Workday's error quotes the value it rejected ('Invalid Date: 10//') and keeps it until the next save: with every section
  // filled and a different value now, it is stale (Parsons' disability-form date read 10/24/2026 and was reported
  // uncommitted five times, 2026-10-24)
  const quoted = (fieldText.match(/invalid date:\s*([0-9\/]*)/i) || [])[1];
  const nowVal = out.map(o => o.value || '').join('/');
  const stale = quoted !== undefined && out.length >= 2 && out.every(o => (o.value || '') !== '') && quoted !== nowVal;
  return JSON.stringify({ found: out.length > 0, sections: out, text: boxText.slice(0, 160),
                          invalid: !stale && (/invalid date/i.test(boxText) || /invalid date/i.test(fieldText) || ariaBad) });
}
"""

# Session-timeout prompts ("Are You Still With Us?" on Oracle, "Your session is about to expire"): press the
# stay / continue button. Never a sign-out, cancel or anything submit-like.
JS_KEEPALIVE_DIALOG = r"""
() => {
""" + _COMMON + r"""
  const ASK = /(are you still (with us|there|here)|still (with us|there)\?|session (is )?(about to |going to )?(expire|time ?out)|session time ?out|you('| ha)ve been inactive|due to inactivity|extend (your )?session)/i;
  const STAY = /^(yes|ok|okay|continue|continue (session|working|application)|stay( signed in| logged in)?|keep (me )?(signed|logged) in|i'?m (still )?here|extend( session)?|resume|yes,? (i'?m here|continue))$/i;
  const NEVER = /(sign ?out|log ?out|logout|end session|cancel|no\b|submit|apply|finish|send|delete|discard|leave)/i;
  for (const root of allRoots(document)) {
    const boxes = Array.from(root.querySelectorAll('[role="dialog"], [role="alertdialog"], dialog[open], [aria-modal="true"], [class*="modal" i], [class*="dialog" i]')).filter(vis);
    for (const d of boxes) {
      const t = txt(d);
      if (!t || t.length > 1200 || !ASK.test(t)) continue;
      const btn = Array.from(d.querySelectorAll('button, [role="button"], a, input[type="button"]')).filter(vis)
        .find(b => { const l = (txt(b) || b.value || b.getAttribute('aria-label') || '').trim(); return STAY.test(l) && !NEVER.test(l); });
      if (!btn) return JSON.stringify({ dismissed: false, text: t.slice(0, 160), reason: 'no stay button' });
      btn.click();
      return JSON.stringify({ dismissed: true, text: t.slice(0, 160), button: (txt(btn) || btn.value || '').slice(0, 40) });
    }
  }
  return JSON.stringify({ dismissed: false });
}
"""

JS_PAGE_TEXT = r"""
(maxChars) => {
  let t = document.body ? document.body.innerText : '';
  // text drawn inside open shadow roots is not in innerText: SmartRecruiters' fields and its file chip (ServiceNow,
  // 2026-10-21: the gate called the attached résumé missing)
  const extra = [];
  const walk = (root, depth) => {
    for (const el of root.querySelectorAll('*')) {
      if (!el.shadowRoot || depth > 6) continue;
      const s = Array.from(el.shadowRoot.children).filter(c => !/^(STYLE|SCRIPT|LINK|TEMPLATE)$/.test(c.tagName))
        .map(c => c.innerText || '').join(' ').replace(/\s+/g, ' ').trim();
      if (s) extra.push(s);
      walk(el.shadowRoot, depth + 1);
    }
  };
  try { walk(document, 0); } catch (e) {}
  if (extra.length) t += '\n' + extra.join('\n');
  return t.replace(/[ \t]+/g, ' ').replace(/\n{3,}/g, '\n\n').slice(0, maxChars);
}
"""


# The separate open button beside a combobox input (Ashby's university search on Sierra, 2026-10-18: clicking or typing
# into the input opens nothing; the "Open university dropdown" button beside it does). Returns its centre, or ok=false.
JS_OPEN_TOGGLE = r"""(ref) => {
  const el = document.querySelector('[data-goat-ref="' + ref + '"]');
  if (!el) return {ok: false};
  const TOGGLE = /(open|toggle|show|expand)\b.{0,30}\b(dropdown|menu|list|options|select)|\bdropdown\b|show options|chevron|caret|toggle[-_ ]?button|_toggle/i;
  const text = (x) => [x.getAttribute('aria-label') || '', x.getAttribute('title') || '',
                       (typeof x.className === 'string' ? x.className : (x.className && x.className.baseVal) || '')].join(' ');
  let box = el.parentElement;
  for (let i = 0; i < 4 && box; i++, box = box.parentElement) {
    // a container that holds another field is wider than this control: its buttons belong to someone else
    const others = Array.from(box.querySelectorAll('input:not([type="hidden"]), select, textarea, [role="combobox"]'))
      .filter(x => x !== el && !el.contains(x) && !x.contains(el));
    if (others.length) break;
    const b = Array.from(box.querySelectorAll('button, [role="button"]')).find(x => x !== el && !x.contains(el) && TOGGLE.test(text(x)));
    if (b) {
      b.scrollIntoView({block: 'center', inline: 'nearest'});
      const r = b.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) return {ok: true, x: r.left + r.width / 2, y: r.top + r.height / 2, label: (b.getAttribute('aria-label') || '').slice(0, 80)};
    }
    if (box.matches && box.matches('form, fieldset')) break;
  }
  return {ok: false};
}"""


# The checkboxes / radios that answer the same question as `ref` when the snapshot could not group them (Workday's race
# checkboxes have no shared name or legend, HCSC 2026-10-19): the nearest container holding two or more of them.
JS_CHOICE_SIBLINGS = r"""(ref) => {
""" + _COMMON + r"""
  const el = findRef(ref);
  if (!el || !(el.type === 'checkbox' || el.type === 'radio')) return JSON.stringify([]);
  let box = el.parentElement;
  for (let i = 0; i < 6 && box; i++, box = box.parentElement) {
    const peers = Array.from(box.querySelectorAll('input[type="checkbox"], input[type="radio"]'));
    if (peers.length >= 2) {
      return JSON.stringify(peers.map(p => ({ ref: p.getAttribute('data-goat-ref') || '', label: labelFor(p), checked: !!p.checked })).filter(p => p.ref));
    }
  }
  return JSON.stringify([]);
}"""


# Ashby keeps each answer twice: the value on screen (component state) and the value its server stored (`savedValue`,
# from the form render). A save lost to overlapping requests leaves the answer on screen but missing at submit:
# "Missing entry for required field" (Sierra, 2026-10-19). Lists every field whose stored value differs from the
# one shown (a text box only when nothing was stored at all).
JS_ASHBY_UNSAVED = r"""() => {
  const out = [];
  const norm = v => v === undefined || v === null ? '' : (Array.isArray(v) ? v.map(x => String(x)).sort().join(' | ') : String(v).trim());
  for (const entry of document.querySelectorAll('[data-field-path]')) {
    const el = entry.querySelector('input:not([type="file"]):not([type="hidden"]), textarea, select, button[aria-pressed], [role="combobox"]');
    if (!el) continue;
    const fk = Object.keys(el).find(k => k.startsWith('__reactFiber'));
    let f = fk ? el[fk] : null;
    let shown, hasShown = false, choice = false, saved, found = false, title = '';
    for (let i = 0; i < 25 && f; i++, f = f.return) {
      const p = f.memoizedProps || {};
      if (typeof p !== 'object') continue;
      if ('savedValue' in p) { saved = p.savedValue; found = true; title = (p.field && (p.field.title || p.field.path)) || ''; break; }
      if ('labeledValues' in p || 'selectableValues' in p) { choice = true; if ('value' in p) { shown = p.value; hasShown = true; } continue; }
      if (!hasShown && typeof f.type !== 'string' && 'value' in p) { shown = p.value; hasShown = true; }
    }
    if (!found || !hasShown) continue;
    const s = norm(shown), v = norm(saved);
    if (s === v) continue;
    if (!choice && typeof shown !== 'boolean' && (v !== '' || s === '')) continue;
    const radios = entry.querySelectorAll('input[type="radio"]').length;
    out.push({ path: entry.getAttribute('data-field-path'), title: String(title).slice(0, 120), shown: s.slice(0, 80), saved: v.slice(0, 80),
               kind: radios >= 2 ? 'radio' : (choice ? 'choice' : (typeof shown === 'boolean' ? 'toggle' : 'text')),
               refs: Array.from(entry.querySelectorAll('[data-goat-ref]')).map(e => e.getAttribute('data-goat-ref')).slice(0, 16) });
  }
  return JSON.stringify(out);
}"""

# Make Ashby store an answer it shows but lost: 'away' ticks another radio of the field (returns the index that was
# ticked), 'back' ticks that index again, 'blur' focuses and leaves a text box (its blur handler saves).
JS_ASHBY_REPICK = r"""([path, step, idx]) => {
  const entry = Array.from(document.querySelectorAll('[data-field-path]')).find(e => e.getAttribute('data-field-path') === path);
  if (!entry) return JSON.stringify({ ok: false, error: 'field not found' });
  if (step === 'blur') {
    const el = entry.querySelector('input:not([type="file"]):not([type="hidden"]):not([type="radio"]):not([type="checkbox"]), textarea');
    if (!el) return JSON.stringify({ ok: false, error: 'no text box' });
    el.focus(); el.blur();
    el.dispatchEvent(new FocusEvent('focusout', { bubbles: true }));
    return JSON.stringify({ ok: true });
  }
  const radios = Array.from(entry.querySelectorAll('input[type="radio"]'));
  if (radios.length < 2) return JSON.stringify({ ok: false, error: 'not a radio group' });
  if (step === 'away') {
    const cur = radios.findIndex(r => r.checked);
    if (cur < 0) return JSON.stringify({ ok: false, error: 'nothing picked' });
    const other = radios.find((r, i) => i !== cur && !r.disabled);
    if (!other) return JSON.stringify({ ok: false, error: 'no other option' });
    other.click();
    return JSON.stringify({ ok: true, idx: cur });
  }
  const r = radios[idx];
  if (!r) return JSON.stringify({ ok: false, error: 'option gone' });
  if (!r.checked) r.click();
  return JSON.stringify({ ok: r.checked, idx });
}"""

# Whether the element [data-goat-ref=ref] sits inside one of the custom elements named in `tags`, crossing shadow roots:
# SmartRecruiters' 'autocomplete your application' résumé box is inside <oc-apply-with-resume> (Wellmark, 2026-10-21).
JS_INSIDE_TAG = r"""
([ref, tags]) => {
""" + _COMMON + r"""
  const want = tags.map(t => t.toLowerCase());
  let n = findRef(ref);
  for (let i = 0; n && i < 80; i++) {
    if (n.tagName && want.includes(n.tagName.toLowerCase())) return n.tagName.toLowerCase();
    n = n.parentNode && n.parentNode.nodeType === 11 ? n.parentNode.host : n.parentNode;
  }
  return '';
}
"""


# Whether a file name shows anywhere the page text cannot see: inside shadow roots or in an aria-label (SmartRecruiters'
# drop zone keeps 'Delete file Surya_Thirukonda_Resume_SWE.pdf' in its shadow root, Wellmark 2026-10-21).
JS_FILE_SHOWN = r"""
(names) => {
""" + _COMMON + r"""
  const want = names.map(n => String(n || '').toLowerCase()).filter(Boolean);
  const hit = (t) => { t = String(t || '').toLowerCase(); return want.some(w => t.includes(w)); };
  for (const root of allRoots(document)) {
    if (root !== document && hit(root.textContent)) return true;
    try { for (const e of root.querySelectorAll('[aria-label]')) if (hit(e.getAttribute('aria-label'))) return true; } catch (e) {}
  }
  return false;
}
"""
