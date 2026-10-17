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
  const txt = (el) => ((el && (el.innerText || el.textContent)) || '').replace(/\s+/g, ' ').trim();
  const PLACEHOLDER = /^(select one|select\.\.\.|select…|select|choose one|choose|search|please select|-- select --|--|)$/i;
  function labelFor(el) {
    const doc = el.ownerDocument; let t = '';
    if (el.id) { try { const l = doc.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) t = txt(l); } catch (e) {} }
    if (!t) { const l = el.closest('label'); if (l) t = txt(l); }
    if (!t) t = el.getAttribute('aria-label') || '';
    if (!t) { const ids = el.getAttribute('aria-labelledby'); if (ids) { t = ids.split(/\s+/).map(i => { const n = doc.getElementById(i); return n ? txt(n) : ''; }).filter(Boolean).join(' '); } }
    if (!t) { const ff = el.closest('[data-automation-id^="formField"], .field, .form-group, .application-field, [class*="field" i], fieldset'); if (ff) { const l = ff.querySelector('label, legend, [class*="label" i]'); if (l && !l.contains(el)) t = txt(l); } }
    if (!t) t = el.getAttribute('placeholder') || el.getAttribute('name') || el.id || '';
    return t.replace(/\s+/g, ' ').trim().slice(0, 200);
  }
  function* allRoots(root) {
    yield root;
    const els = root.querySelectorAll ? root.querySelectorAll('*') : [];
    for (const el of els) {
      if (el.shadowRoot) yield* allRoots(el.shadowRoot);
      if (el.tagName === 'IFRAME') { try { if (el.contentDocument) yield* allRoots(el.contentDocument); } catch (e) {} }
    }
  }
  function findRef(ref) {
    for (const root of allRoots(document)) {
      try { const el = root.querySelector('[data-goat-ref="' + ref + '"]'); if (el) return el; } catch (e) {}
    }
    return null;
  }
"""

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
  const SEL = 'input, textarea, select, button, a[href], [role="button"], [role="combobox"], [role="listbox"], [role="checkbox"], [role="radio"], [role="switch"], [role="tab"], [role="menuitem"], [role="option"], [contenteditable="true"], [data-automation-id="selectWidget"], [data-automation-id="promptOption"], [aria-haspopup]';

  function groupLabel(el) {
    const fs = el.closest('fieldset, [role="group"], [role="radiogroup"], [data-automation-id^="formField"], .field, .form-group, .application-question, [class*="question" i]');
    if (fs) {
      const lg = fs.querySelector('legend, [class*="legend" i], [id$="-label"], [class*="question" i] > :first-child, label:not([for]), .label, [class*="label" i]');
      if (lg && !lg.contains(el)) { const t = txt(lg); if (t && t.length < 300) return t; }
      const lb = fs.getAttribute('aria-labelledby'); if (lb) { const n = document.getElementById(lb.split(' ')[0]); if (n) return txt(n).slice(0, 300); }
    }
    return '';
  }

  for (const root of allRoots(document)) {
    let els = []; try { els = root.querySelectorAll(SEL); } catch (e) { continue; }
    for (const el of els) {
      if (seen.has(el)) continue; seen.add(el);
      const tag = el.tagName.toLowerCase();
      const type = (el.getAttribute('type') || '').toLowerCase();
      if (type === 'hidden') continue;
      // Site chrome (header/nav menus, account and language switchers) is not part of the form.
      if (el.closest('header, nav, [role="banner"], [role="navigation"], [data-automation-id="utilityButtonBar"], [data-automation-id="header"], footer, [role="contentinfo"]')) {
        if (!(tag === 'input' || tag === 'textarea' || tag === 'select')) continue;
      }
      const isFile = type === 'file';
      // react-select style inputs are rendered with opacity 0 and width 1px inside a visible control;
      // treat them as visible when their control container is, and position them on the container.
      const comboLike = tag === 'input' && (el.getAttribute('aria-autocomplete') || el.getAttribute('aria-haspopup') || /react-select|select__input|__input/i.test(el.className || ''));
      const choiceLike = tag === 'input' && (type === 'checkbox' || type === 'radio');
      let posEl = el;
      if (!vis(el) && !isFile) {
        // Custom-styled radios/checkboxes hide the real input (opacity 0 / sr-only) inside a visible label.
        let ctrl = comboLike ? el.closest('[class*="__control"], [class*="control"], [class*="Control"], [class*="__value-container"]') : null;
        if (!ctrl && choiceLike) { ctrl = el.closest('label'); if (!ctrl && el.id) { try { ctrl = el.ownerDocument.querySelector('label[for="' + CSS.escape(el.id) + '"]'); } catch (e) {} } if (!ctrl) ctrl = el.parentElement; }
        if (!(ctrl && vis(ctrl))) continue;
        posEl = ctrl;
      }
      const role = el.getAttribute('role') || '';
      const item = { ref: ref(el), tag, type, role, label: labelFor(el), bbox: bbox(posEl), inview: inView(posEl) };
      item.required = !!(el.required || el.getAttribute('aria-required') === 'true' || /required|\*/.test(el.getAttribute('aria-label') || '') );
      item.disabled = !!(el.disabled || el.getAttribute('aria-disabled') === 'true');
      item.invalid = el.getAttribute('aria-invalid') === 'true';
      item.placeholder = el.getAttribute('placeholder') || '';
      if (tag === 'input' || tag === 'textarea') {
        if (type === 'password') item.value = el.value ? '(set)' : '';
        else if (isFile) {
          item.value = el.files && el.files.length ? el.files[0].name : ''; item.hidden = !vis(el);
          item.name = el.getAttribute('name') || el.id || '';
          // Portals label hidden file inputs with the button text ("Attach"); the input name says which slot it is.
          if (item.name && !/resume|cv|cover|transcript/i.test(item.label)) item.label = item.name + ' (' + (item.label || 'file') + ')';
        }
        else if (type === 'checkbox' || type === 'radio') { item.checked = el.checked; }
        else item.value = (el.value || '').slice(0, 300);
        if (!item.value) {
          const ms = el.closest('[data-automation-id="multiSelectContainer"]');
          const picked = ms ? Array.from(ms.querySelectorAll('[data-automation-id="selectedItem"]')).map(x => txt(x)).filter(Boolean) : [];
          if (picked.length) { item.value = picked.join('; ').slice(0, 300); item.selected_items = picked.length; }
        }
        const isCombo = role === 'combobox' || el.getAttribute('aria-autocomplete') || el.hasAttribute('aria-expanded') || /react-select|__input/i.test(el.id || el.className || '');
        if (isCombo && !item.value) {
          let c = el.closest('[class*="__control"], [class*="control"], [class*="Control"], [class*="__value-container"]');
          if (!c || !txt(c)) { c = null; let p = el.parentElement; for (let i = 0; i < 5 && p; i++, p = p.parentElement) { const t = txt(p); if (t && t.length < 240) { c = p; break; } } }
          const shown = c ? txt(c) : '';
          if (shown && !PLACEHOLDER.test(shown)) item.shown = shown.slice(0, 200);
          item.combo = true;
        }
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
      } else if (tag === 'button' || role === 'button' || el.hasAttribute('aria-haspopup') || el.getAttribute('data-automation-id') === 'selectWidget') {
        const shown = txt(el).slice(0, 120);
        const isDropdown = el.hasAttribute('aria-haspopup') || el.getAttribute('data-automation-id') === 'selectWidget' || role === 'combobox';
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
          const nFilled = Array.from(document.querySelectorAll('input:not([type=hidden]):not([type=file]):not([type=checkbox]):not([type=radio]):not([type=submit]), textarea')).filter(i => (i.value || '').trim()).length;
          const onAccountPage = Array.from(document.querySelectorAll('input[type=password]')).some(i => { const r = i.getBoundingClientRect(); return r.width > 0 && r.height > 0; });
          const accountBtn = onAccountPage || /^(create account|create an account|sign in|sign-in|log in|login|continue|next|save and continue|save & continue|verify|verify email|send code|resend|back)$/i.test(item.label);
          item.submitish = !accountBtn && (/^(submit|submit application|submit my application|send application|finish|complete application|complete)$/i.test(item.label)
            || (/^(apply|apply now|apply for this job|apply to this job)$/i.test(item.label) && nFilled >= 3)
            || (type === 'submit' && nFilled >= 3));
          // Toggle buttons (Ashby Yes/No, segmented choices) carry aria-pressed/aria-checked: an answer, not navigation.
          const pressed = el.getAttribute('aria-pressed') ?? el.getAttribute('aria-checked');
          if (pressed !== null) {
            item.tag = 'toggle'; item.pressed = pressed === 'true';
            item.group = groupLabel(el) || (() => { let p = el.parentElement; for (let i = 0; i < 4 && p; i++, p = p.parentElement) { const t = txt(p); if (t.length > item.label.length + 4 && t.length < 400) return t.replace(item.label, '').replace(/\s+/g, ' ').trim().slice(0, 200); } return ''; })();
          }
        }
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
  const headings = [];
  for (const h of document.querySelectorAll('h1, h2, h3, [role="heading"]')) { if (vis(h)) { const t = txt(h); if (t && t.length < 160) headings.push(t); } if (headings.length >= 12) break; }
  const bodyText = (document.body ? document.body.innerText : '').replace(/\s+/g, ' ');
  return JSON.stringify({ url: location.href, title: document.title, headings, items: items.slice(0, maxItems),
    groups: groupList, total: items.length, scrollY: Math.round(scrollY), pageHeight: Math.round(document.documentElement.scrollHeight), vw: innerWidth, vh: innerHeight,
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
  const filledInputs = Array.from(document.querySelectorAll('input:not([type=hidden]):not([type=file]):not([type=checkbox]):not([type=radio]):not([type=submit]), textarea')).filter(i => (i.value || '').trim()).length;
  const formFilled = filledInputs >= 3;
  // A visible password box means a sign-in / create-account page: its "Submit" button (Workday lays a hidden
  // "Submit" exactly over "Sign In") signs in; it never submits an application.
  const accountPage = Array.from(document.querySelectorAll('input[type=password]')).some(i => { const r = i.getBoundingClientRect(); return r.width > 0 && r.height > 0; });
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
    const isBtn = tag === 'button' || tag === 'a' || p.getAttribute('role') === 'button' || (tag === 'input' && (type === 'submit' || type === 'button'));
    if (isBtn) {
      const label = (tag === 'input' ? (p.value || '') : (t || p.getAttribute('aria-label') || '')).replace(/\s+/g, ' ').trim();
      // Account-flow buttons (Create Account, Sign In, Continue, Verify) are navigation, even when type=submit.
      const ACCOUNT = /^(create account|create an account|sign in|sign-in|log in|login|continue|next|save and continue|save & continue|verify|verify email|send code|resend|back)$/i;
      if (ACCOUNT.test(label)) break;
      if (accountPage) break;
      if (SUBMIT.test(label) || (APPLY.test(label) && formFilled) || (type === 'submit' && formFilled)) { submit = true; why = 'submit control: ' + label; }
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

# --------------------------------------------------------------------------- page errors (Hermes, proven)
JS_PAGE_ERRORS = r"""
() => {
  const ERR = /(no\s+\S+\s+found|not\s+a\s+valid|invalid|must\s+be|must\s+contain|cannot\s+be\s+(blank|empty)|can't\s+be\s+(blank|empty)|is\s+required|this\s+field\s+is\s+required|please\s+(enter|select|provide|choose|complete|correct|fix)|try\s+(entering|again|a\s+different)|failed|too\s+(short|long)|does\s+not\s+match|already\s+(exists|taken|registered|in\s+use)|incorrect|wrong\s+(email|password|username)|(account|user)\s+(might\s+be|is|has\s+been)\s+locked|unable\s+to\s+(sign|log)\s+in|could\s+not\s+(sign|log)\s+in)/i;
  const SKIP = /^(required|\*|optional|required\s*\*|\*\s*required)$/i;
  const STATUSY = /(loaded|loading|please\s+wait|saved|updated|added|removed|expanded|collapsed|in\s+progress|step\s+\d|page\s+\d|results?\s+(found|updated)|showing\s+\d|selected)/i;
  const INSTRUCTIONAL = /(if\s+applicable|e\.g\.|for\s+example|\(optional\)|as\s+it\s+appears|do\s+not\s+include|maximum\s+of|up\s+to\s+\d|characters?\s+remaining|format:)/i;
  const alerts = [], errorText = [], emptyRequired = [];
  const seenRoots = new Set(), seenText = new Set();
  function vis(el) { try { const r = el.getBoundingClientRect(); const s = el.ownerDocument.defaultView.getComputedStyle(el);
      return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && parseFloat(s.opacity || '1') > 0.01; } catch (e) { return false; } }
  function txt(el) { return (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim(); }
  function nearestLabel(el) {
    let p = el, hops = 0;
    while (p && hops < 6) { const l = p.querySelector ? p.querySelector('label, [class*="label" i]') : null; if (l) { const t = txt(l); if (t && t.length < 120) return t.slice(0, 120); } p = p.parentElement; hops++; }
    const al = el.getAttribute && (el.getAttribute('aria-label') || ''); return (al || '').slice(0, 120);
  }
  function push(arr, obj, key) { if (seenText.has(key)) return; seenText.add(key); arr.push(obj); }
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
      if (!vis(el)) continue;
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
      if (el.closest && el.closest('.apply-flow-block__header')) continue;
      const t = txt(el);
      if (!t || t.length > 300 || SKIP.test(t)) continue;
      const promptKey = s => String(s || '').toLowerCase().replace(/[\s.:;!?]+$/g, '').trim();
      const isFieldPrompt = Array.from(document.querySelectorAll('label, legend')).some(l => promptKey(txt(l)) === promptKey(t));
      if (isFieldPrompt) continue;
      if (!ERR.test(t)) continue;
      if (INSTRUCTIONAL.test(t)) continue;
      if (staleRequiredForSatisfiedChoiceGroup(el, t)) continue;
      push(errorText, { text: t.slice(0, 300), tag: el.tagName.toLowerCase(), near: nearestLabel(el) }, 'e:' + t);
    }
    let ctl = []; try { ctl = root.querySelectorAll('input, textarea, select'); } catch (e) { ctl = []; }
    for (const el of ctl) {
      if (el.type === 'hidden' || !vis(el)) continue;
      const req = el.required || el.getAttribute('aria-required') === 'true';
      if (!req) continue;
      let empty;
      if (el.type === 'checkbox' || el.type === 'radio') {
        const peers = el.name ? Array.from(root.querySelectorAll('input[type="' + el.type + '"][name="' + CSS.escape(el.name) + '"]')) : [el];
        empty = !peers.some(x => x.checked);
      } else if (el.type === 'file') empty = !(el.files && el.files.length);
      else if (el.tagName === 'SELECT') empty = !el.value || !el.selectedOptions.length;
      else {
        empty = !String(el.value || '').trim();
        if (empty) {
          const ms = el.closest('[data-automation-id="multiSelectContainer"]');
          if (ms && ms.querySelector('[data-automation-id="selectedItem"]')) empty = false;
        }
        if (empty) {
          const isCombo = el.getAttribute('role') === 'combobox' || el.getAttribute('aria-autocomplete') || el.hasAttribute('aria-expanded') || /react-select|__input/i.test(el.id || el.className || '');
          if (isCombo) {
            let c = el.closest('[class*="__control"], [class*="control"], [class*="Control"], [class*="__value-container"]');
            if (!c || !txt(c)) { c = null; let p = el.parentElement; for (let i = 0; i < 5 && p; i++, p = p.parentElement) { const t = txt(p); if (t && t.length < 240) { c = p; break; } } }
            const shown = c ? txt(c) : '';
            if (shown && !/^(select one|select\.\.\.|select|choose one|choose|search)$/i.test(shown)) empty = false;
          }
        }
      }
      if (!empty) continue;
      const lab = nearestLabel(el) || el.name || el.id || '(unlabelled)';
      push(emptyRequired, { label: lab, type: el.type || el.tagName.toLowerCase(), name: el.name || el.id || '', ref: el.getAttribute('data-goat-ref') || '' }, 'r:' + lab + ':' + (el.name || el.id || ''));
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
  return JSON.stringify({ url: location.href, alerts: alerts.slice(0, 40), error_text: errorText.slice(0, 40), empty_required: emptyRequired.slice(0, 40) });
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
  const want = (label || '').trim().toLowerCase();
  const DEF = /^(submit|submit application|apply|apply now|send application|finish|complete|complete application|submit my application)$/i;
  const cands = Array.from(document.querySelectorAll('button, input[type=submit], input[type=button], [role=button], a[href="#"]'));
  for (const b of cands) {
    const t = ((b.innerText || b.value || b.getAttribute('aria-label') || '') + '').replace(/\s+/g, ' ').trim();
    const ok = want ? t.toLowerCase() === want : DEF.test(t);
    if (!ok) continue;
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
  const textChallenge = /(i'?m not a robot|verify (that )?you are (a )?human|complete the (security )?check|select all images|prove you'?re not a robot)/i.test(body);
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
        if (type === 'radio') { if (!el.checked) el.click(); }
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
      } else if (tag === 'button' && (el.hasAttribute('aria-pressed') || el.hasAttribute('aria-checked'))) {
        const want = (f.value === true || /^(true|yes|on|checked|1|pressed)$/i.test(String(f.value)));
        const isOn = () => (el.getAttribute('aria-pressed') ?? el.getAttribute('aria-checked')) === 'true';
        if (isOn() !== want) el.click();
        res.readback = isOn() ? 'pressed' : 'unpressed'; res.ok = isOn() === want;
      } else if (tag === 'input' && type === 'file') {
        res.error = 'use upload_resume'; out.push(res); continue;
      } else if (tag === 'input' || tag === 'textarea') {
        if (type === 'password') { res.error = 'password fields are filled by the credentials helper'; out.push(res); continue; }
        setNative(el, String(f.value));
        res.readback = (el.value || '').slice(0, 300); res.ok = (el.value || '') === String(f.value) || (type === 'number' && Number(el.value) === Number(f.value));
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
  else if ('value' in el) value = (el.value || '').slice(0, 300);
  else value = txt(el).slice(0, 300);
  let shown = '';
  const c = el.closest('[class*="__control"], [class*="control"], [class*="Control"], [data-automation-id^="formField"], .field, .form-group');
  if (c) shown = txt(c).slice(0, 300);
  el.scrollIntoView({ block: 'center', inline: 'nearest' });
  const r2 = el.getBoundingClientRect();
  return JSON.stringify({ found: true, tag, type, label: labelFor(el), value, shown, x: Math.round(r2.x + r2.width / 2), y: Math.round(r2.y + r2.height / 2), w: Math.round(r2.width), h: Math.round(r2.height),
    disabled: !!el.disabled, expanded: el.getAttribute('aria-expanded'), invalid: el.getAttribute('aria-invalid') === 'true' });
}
"""

JS_LIST_OPTIONS = r"""
() => {
  const vis = (el) => { const r = el.getBoundingClientRect(); return r.width > 1 && r.height > 1; };
  const txt = (el) => ((el.getAttribute && el.getAttribute('data-automation-label')) || el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  const opts = Array.from(document.querySelectorAll('[role="option"], [data-automation-id="promptOption"], [data-automation-id="menuItem"], li[role="presentation"] div[role="option"], .select__option, [class*="__option"], [class*="option" i][id*="option" i], ul[role="listbox"] li, [role="listbox"] [role="option"]'));
  const out = [];
  for (const o of opts) {
    if (!vis(o)) continue;
    const r = o.getBoundingClientRect();
    const t = txt(o).slice(0, 160); if (!t) continue;
    out.push({ text: t, x: Math.round(r.x + Math.min(30, r.width / 2)), y: Math.round(r.y + r.height / 2), selected: o.getAttribute('aria-selected') === 'true' });
  }
  return JSON.stringify(out.slice(0, 400));
}
"""

# Bring one option of an open list into view (long Workday lists scroll inside the popup) and return
# where to click it now. Prefers an option inside an open listbox over a chip of an already-chosen value.
JS_SCROLL_OPTION = r"""
(text) => {
  const txt = (el) => ((el.getAttribute && el.getAttribute('data-automation-label')) || el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  const opts = Array.from(document.querySelectorAll('[role="option"], [data-automation-id="promptOption"], [data-automation-id="menuItem"], .select__option, [class*="__option"], ul[role="listbox"] li'))
    .filter(o => txt(o).slice(0, 160) === text && o.getBoundingClientRect().width > 1);
  if (!opts.length) return JSON.stringify({ ok: false });
  const inList = opts.filter(o => !o.closest('[data-automation-id="selectedItemList"]'));
  const el = (inList.length ? inList : opts)[inList.length ? inList.length - 1 : 0];
  el.scrollIntoView({ block: 'center', inline: 'nearest' });
  const r = el.getBoundingClientRect();
  return JSON.stringify({ ok: true, x: Math.round(r.x + Math.min(30, r.width / 2)), y: Math.round(r.y + r.height / 2) });
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
  return JSON.stringify({ found: out.length > 0, sections: out, text: boxText.slice(0, 160),
                          invalid: /invalid date/i.test(boxText) });
}
"""

JS_PAGE_TEXT = r"""
(maxChars) => (document.body ? document.body.innerText : '').replace(/[ \t]+/g, ' ').replace(/\n{3,}/g, '\n\n').slice(0, maxChars)
"""
