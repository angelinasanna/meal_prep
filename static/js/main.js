// ── Onboarding multi-step ────────────────────────────────────
(function () {
  const sections = document.querySelectorAll('.step-section');
  const nextBtns = document.querySelectorAll('[data-next-step]');
  const backBtns = document.querySelectorAll('[data-prev-step]');
  const stepDots = document.querySelectorAll('[data-step-dot]');

  if (!sections.length) return;

  let current = 0;

  function show(idx) {
    sections.forEach((s, i) => s.classList.toggle('active', i === idx));
    stepDots.forEach((d, i) => {
      d.classList.toggle('active', i === idx);
      d.classList.toggle('done', i < idx);
      if (i < idx) d.textContent = '✓';
      else d.textContent = i + 1;
    });
    current = idx;
  }

  nextBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      if (validateStep(current)) show(current + 1);
    });
  });

  backBtns.forEach(btn => {
    btn.addEventListener('click', () => show(current - 1));
  });

  function validateStep(idx) {
    if (idx === 0) {
      const anyChecked = document.querySelectorAll('input[name="meal_types"]:checked');
      if (!anyChecked.length) {
        shake(sections[0]);
        return false;
      }
    }
    return true;
  }

  function shake(el) {
    el.style.animation = 'none';
    el.offsetHeight;
    el.style.animation = 'shake .3s ease';
  }

  show(0);
})();

// ── Meal type row toggle (driven by native checkbox change) ──
document.querySelectorAll('.meal-type-row').forEach(row => {
  const cb = row.querySelector('input[type="checkbox"]');
  if (!cb) return;
  cb.addEventListener('change', () => row.classList.toggle('checked', cb.checked));
});

// ── Servings stepper ─────────────────────────────────────────
(function () {
  const display = document.getElementById('servings-display');
  const hidden = document.getElementById('servings-hidden');
  const dec = document.getElementById('servings-dec');
  const inc = document.getElementById('servings-inc');
  if (!display) return;

  let val = parseInt(hidden.value) || 2;

  function update(v) {
    val = Math.max(1, Math.min(20, v));
    display.textContent = val;
    hidden.value = val;
  }

  dec.addEventListener('click', () => update(val - 1));
  inc.addEventListener('click', () => update(val + 1));
})();

// ── Tag ingredient input ─────────────────────────────────────
(function () {
  const area = document.getElementById('tag-input-area');
  if (!area) return;

  const tagList = document.getElementById('tag-list');
  const textInput = document.getElementById('tag-text');
  const hiddenInput = document.getElementById('ingredients-hidden');
  let tags = [];

  function renderTags() {
    tagList.innerHTML = '';
    tags.forEach((t, i) => {
      const span = document.createElement('span');
      span.className = 'tag';
      span.innerHTML = `${t} <button class="tag-remove" type="button" data-idx="${i}" aria-label="Remove ${t}">×</button>`;
      tagList.appendChild(span);
    });
    hiddenInput.value = tags.join(',');
  }

  function addTag(val) {
    const clean = val.trim().toLowerCase();
    if (clean && !tags.includes(clean)) {
      tags.push(clean);
      renderTags();
    }
  }

  textInput.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ',') {
      e.preventDefault();
      addTag(textInput.value);
      textInput.value = '';
    }
    if (e.key === 'Backspace' && !textInput.value && tags.length) {
      tags.pop();
      renderTags();
    }
  });

  textInput.addEventListener('blur', () => {
    if (textInput.value.trim()) {
      addTag(textInput.value);
      textInput.value = '';
    }
  });

  tagList.addEventListener('click', e => {
    const btn = e.target.closest('.tag-remove');
    if (btn) {
      tags.splice(parseInt(btn.dataset.idx), 1);
      renderTags();
    }
  });

  area.addEventListener('click', () => textInput.focus());
})();

// ── Grocery checkbox (non-htmx fallback label) ───────────────
document.querySelectorAll('.grocery-item').forEach(item => {
  const cb = item.querySelector('.grocery-checkbox');
  if (!cb) return;
  if (cb.checked) item.classList.add('done');
  cb.addEventListener('change', () => item.classList.toggle('done', cb.checked));
});

// ── Shake keyframe (injected once) ───────────────────────────
if (!document.getElementById('shake-style')) {
  const s = document.createElement('style');
  s.id = 'shake-style';
  s.textContent = `@keyframes shake {
    0%,100%{transform:translateX(0)}
    20%{transform:translateX(-6px)}
    40%{transform:translateX(6px)}
    60%{transform:translateX(-4px)}
    80%{transform:translateX(4px)}
  }`;
  document.head.appendChild(s);
}
