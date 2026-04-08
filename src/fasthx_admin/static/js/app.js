// fasthx-admin - Minimal JS (HTMX handles most interactions)

// Theme switcher
function toggleTheme() {
    var html = document.documentElement;
    var current = html.getAttribute('data-bs-theme');
    var next = current === 'dark' ? 'light' : 'dark';
    html.setAttribute('data-bs-theme', next);
    localStorage.setItem('theme', next);
    if (typeof restyleAllTomSelects === 'function') restyleAllTomSelects();
}

// Show global loading indicator for HTMX requests
document.addEventListener('htmx:beforeRequest', function (event) {
    var indicator = document.getElementById('global-indicator');
    if (indicator) indicator.style.display = 'inline-block';
});

document.addEventListener('htmx:afterRequest', function (event) {
    var indicator = document.getElementById('global-indicator');
    if (indicator) indicator.style.display = 'none';
});

// Toast notifications — triggered via HX-Trigger: {"showToast": {"message": "...", "type": "success"}}
function showToast(detail) {
    var data = typeof detail === 'string' ? { message: detail } : detail;
    var message = data.message || '';
    var type = data.type || 'info';
    var title = data.title || type.charAt(0).toUpperCase() + type.slice(1);
    var delay = data.delay || 5000;

    var icons = {
        success: 'check-circle-fill',
        danger: 'exclamation-triangle-fill',
        warning: 'exclamation-triangle-fill',
        info: 'info-circle-fill'
    };
    var icon = icons[type] || 'info-circle-fill';

    var toastEl = document.createElement('div');
    toastEl.className = 'toast';
    toastEl.setAttribute('role', 'alert');
    toastEl.innerHTML =
        '<div class="toast-header">' +
        '<i class="bi bi-' + icon + ' text-' + type + ' me-2"></i>' +
        '<strong class="me-auto">' + title + '</strong>' +
        '<button type="button" class="btn-close" data-bs-dismiss="toast"></button>' +
        '</div>' +
        '<div class="toast-body">' + message + '</div>';

    var container = document.getElementById('toast-container');
    if (container) {
        container.appendChild(toastEl);
        var toast = new bootstrap.Toast(toastEl, { delay: delay });
        toast.show();
        toastEl.addEventListener('hidden.bs.toast', function () {
            toastEl.remove();
        });
    }
}

// Modal — triggered via HX-Trigger: {"showModal": {}}
function showModal(detail) {
    var modalEl = document.getElementById('admin-modal');
    if (!modalEl) return;
    var modal = bootstrap.Modal.getOrCreateInstance(modalEl);
    var dialog = modalEl.querySelector('.modal-dialog');
    if (dialog) {
        dialog.classList.remove('modal-lg', 'modal-xl', 'modal-sm');
        if (detail && detail.size) {
            dialog.classList.add(detail.size);
        }
    }
    modal.show();
}

// Dedup guard — prevent double-firing from both native event and afterSettle fallback.
var _toastHandled = null;

// HTMX natively dispatches events from HX-Trigger headers on the body.
// This is the primary listener — works even when the target element is removed from the DOM.
document.body.addEventListener('showToast', function (event) {
    var d = (event.detail && event.detail.value) ? event.detail.value : event.detail;
    var key = JSON.stringify(d);
    if (_toastHandled === key) return;
    _toastHandled = key;
    setTimeout(function () { _toastHandled = null; }, 200);
    showToast(d);
});
document.body.addEventListener('showModal', function (event) {
    var d = (event.detail && event.detail.value) ? event.detail.value : event.detail;
    showModal(d);
});

// Fallback: manually parse HX-Trigger header after swap settles.
// Catches edge cases where the native event might not fire as expected.
document.addEventListener('htmx:afterSettle', function (event) {
    var xhr = event.detail.xhr;
    if (!xhr) return;
    var trigger = xhr.getResponseHeader('HX-Trigger');
    if (!trigger) return;
    try {
        var data = JSON.parse(trigger);
        if (data.showToast) {
            var key = JSON.stringify(data.showToast);
            if (_toastHandled === key) return;
            _toastHandled = key;
            setTimeout(function () { _toastHandled = null; }, 200);
            showToast(data.showToast);
        }
        if (data.showModal) {
            showModal(data.showModal);
        }
    } catch (e) {}
});

// Auto-dismiss alerts after 5 seconds
document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('.alert-dismissible').forEach(function (alert) {
        setTimeout(function () {
            var bsAlert = bootstrap.Alert.getOrCreateInstance(alert);
            bsAlert.close();
        }, 5000);
    });

    // Show any toast passed via cookie (set by server before HX-Redirect)
    var match = document.cookie.match(/(^|;\s*)_toast=([^;]*)/);
    if (match) {
        document.cookie = '_toast=; max-age=0; path=/; samesite=lax';
        try {
            showToast(JSON.parse(decodeURIComponent(match[2])));
        } catch (e) {}
    }
});

// Tom Select - searchable dropdowns for all select.form-select elements
function getTomSelectOptions(el) {
    // Find placeholder text from the empty option
    var emptyOption = el.querySelector('option[value=""]');
    var placeholder = emptyOption ? emptyOption.textContent.trim() : 'Select...';
    // Remove the empty option so it doesn't show as a selectable item
    if (emptyOption) emptyOption.remove();
    // Preserve any pre-selected value (edit forms), otherwise start empty for placeholder
    var selectedOption = el.querySelector('option[selected]');
    var items = selectedOption && selectedOption.value ? [selectedOption.value] : [];
    return {
        create: false,
        sortField: { field: 'text', direction: 'asc' },
        placeholder: placeholder,
        allowEmptyOption: false,
        items: items
    };
}

function getTomSelectColors() {
    var isDark = document.documentElement.getAttribute('data-bs-theme') === 'dark';
    return {
        bg: isDark ? '#1f1f1f' : '#f3f4f6',
        border: isDark ? '#404040' : '#d1d5db',
        color: isDark ? '#ffffff' : '#1f2937'
    };
}

function styleTomSelect(tsInstance) {
    var control = tsInstance.control;
    if (!control) return;
    var c = getTomSelectColors();
    control.style.setProperty('background', c.bg, 'important');
    control.style.setProperty('border', '1px solid ' + c.border, 'important');
    control.style.setProperty('border-radius', '0.375rem');
    control.style.setProperty('color', c.color, 'important');
}

function restyleAllTomSelects() {
    document.querySelectorAll('select.form-select').forEach(function (el) {
        if (el.tomselect) styleTomSelect(el.tomselect);
    });
}

function getAjaxTomSelectOptions(el) {
    var ajaxUrl = el.getAttribute('data-ajax-url');
    var placeholder = el.getAttribute('data-placeholder') || 'Type to search...';
    return {
        create: false,
        placeholder: placeholder,
        allowEmptyOption: false,
        valueField: 'value',
        labelField: 'label',
        searchField: 'label',
        firstUrl: function (query) {
            return ajaxUrl + '?q=' + encodeURIComponent(query);
        },
        shouldLoad: function () { return true; },
        load: function (query, callback) {
            var url = ajaxUrl + '?q=' + encodeURIComponent(query);
            fetch(url)
                .then(function (resp) { return resp.json(); })
                .then(function (data) { callback(data); })
                .catch(function () { callback(); });
        },
        score: function () { return function () { return 1; }; }
    };
}

// Clean up stale Tom Select instances and orphaned wrappers.
// Called before re-initialization after full-page HTMX swaps (hx-boost).
function cleanupTomSelects(root) {
    if (typeof TomSelect === 'undefined') return;
    var container = root || document;
    container.querySelectorAll('select.form-select').forEach(function (el) {
        // Destroy stale instance if the property survived the swap
        if (el.tomselect) {
            try { el.tomselect.destroy(); } catch (e) {}
        }
        // Remove orphaned wrapper siblings left behind by the swap
        var next = el.nextElementSibling;
        if (next && next.classList.contains('ts-wrapper')) {
            next.remove();
        }
        // Remove Tom Select classes from the raw select so it starts clean
        el.classList.remove('tomselected', 'ts-hidden-accessible');
    });
}

function initTomSelect(root) {
    if (typeof TomSelect === 'undefined') return;
    var container = root || document;
    container.querySelectorAll('select.form-select').forEach(function (el) {
        if (el.tomselect) return; // already initialized
        if (el.classList.contains('no-tomselect')) return; // opt-out
        var opts;
        if (el.hasAttribute('data-ajax-url')) {
            opts = getAjaxTomSelectOptions(el);
        } else {
            opts = getTomSelectOptions(el);
        }
        var ts = new TomSelect(el, opts);
        styleTomSelect(ts);
        if (el.hasAttribute('data-ajax-url')) {
            ts.on('focus', function () {
                if (!Object.keys(ts.options).length) {
                    ts.load('');
                }
            });
        }
    });
}

// Sync Tom Select when HTMX swaps options into an existing select
function syncTomSelect(target) {
    if (typeof TomSelect === 'undefined') return;
    var selects = target.matches && target.matches('select.form-select')
        ? [target]
        : [];
    selects.forEach(function (el) {
        if (el.tomselect) {
            // Save the new options HTMX just swapped in before destroying,
            // because destroy() reverts innerHTML to the original state.
            var newHTML = el.innerHTML;
            el.tomselect.destroy();
            el.innerHTML = newHTML;
            var ts = new TomSelect(el, getTomSelectOptions(el));
            styleTomSelect(ts);
        }
    });
}

// Conditional field visibility — show/hide fields based on a checkbox value
function initDependsOn(root) {
    var container = root || document;
    // Find all fields that depend on another field
    var dependents = container.querySelectorAll('[data-depends-on]');
    var controllers = {};
    dependents.forEach(function (el) {
        var key = el.getAttribute('data-depends-on');
        if (!controllers[key]) controllers[key] = [];
        controllers[key].push(el);
    });

    Object.keys(controllers).forEach(function (key) {
        var ctrl = document.getElementById(key);
        if (!ctrl) return;
        var toggle = function () {
            var checked = ctrl.checked;
            controllers[key].forEach(function (el) {
                el.style.display = checked ? '' : 'none';
            });
        };
        toggle(); // set initial state
        ctrl.addEventListener('change', toggle);
    });
}

// Bootstrap tooltips
function initTooltips(root) {
    var container = root || document;
    container.querySelectorAll('[data-bs-toggle="tooltip"]').forEach(function (el) {
        if (!bootstrap.Tooltip.getInstance(el)) {
            new bootstrap.Tooltip(el);
        }
    });
}

// Initialize on page load
document.addEventListener('DOMContentLoaded', function () {
    initTomSelect();
    initDependsOn();
    initTooltips();
});

// Destroy Tom Select instances before HTMX replaces the DOM (prevents orphaned wrappers)
document.addEventListener('htmx:beforeSwap', function (event) {
    var target = event.detail.target;
    if (target === document.body || target === document.documentElement) {
        document.querySelectorAll('select.form-select').forEach(function (el) {
            if (el.tomselect) {
                try { el.tomselect.destroy(); } catch (e) {}
            }
        });
    }
});

// Re-initialize after HTMX swaps new content in
document.addEventListener('htmx:afterSwap', function (event) {
    syncTomSelect(event.detail.target);
    initTomSelect(event.detail.target);
    initDependsOn(event.detail.target);
    initTooltips(event.detail.target);
    // Auto-open modal when content is swapped into it
    var target = event.detail.target;
    if (target && target.closest && target.closest('#admin-modal')) {
        var modalEl = document.getElementById('admin-modal');
        if (modalEl) bootstrap.Modal.getOrCreateInstance(modalEl).show();
    }
});

// Re-initialize after boosted full-page swaps settle (e.g. form validation errors)
document.addEventListener('htmx:afterSettle', function (event) {
    var target = event.detail.target;
    if (target === document.body || target === document.documentElement) {
        cleanupTomSelects();
        initTomSelect();
        initDependsOn();
        initTooltips();
    }
});

// Handle out-of-band swaps (dependent dropdowns with multiple targets)
document.addEventListener('htmx:oobAfterSwap', function (event) {
    syncTomSelect(event.detail.target);
    initTomSelect(event.detail.target);
    initDependsOn(event.detail.target);
    initTooltips(event.detail.target);
});

