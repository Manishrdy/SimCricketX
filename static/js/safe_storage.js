// Persistence is optional: keep page controls working when storage is denied.
(function (window) {
    'use strict';
    var memory = Object.create(null);
    var storageFailed = false;

    window.scxStorage = {
        getItem: function (key) {
            if (!storageFailed) {
                try {
                    var value = window.localStorage.getItem(key);
                    memory[key] = value;
                    return value;
                } catch (_) { storageFailed = true; }
            }
            return Object.prototype.hasOwnProperty.call(memory, key) ? memory[key] : null;
        },
        setItem: function (key, value) {
            memory[key] = String(value);
            if (!storageFailed) {
                try { window.localStorage.setItem(key, memory[key]); }
                catch (_) { storageFailed = true; }
            }
        },
        removeItem: function (key) {
            memory[key] = null;
            if (!storageFailed) {
                try { window.localStorage.removeItem(key); }
                catch (_) { storageFailed = true; }
            }
        }
    };
})(window);
