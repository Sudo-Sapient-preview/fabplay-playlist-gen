/* Shared PostHog bootstrap for Brandbeat pages. */
(function (window, document) {
  'use strict';

  var DEFAULT_HOST = 'https://us.i.posthog.com';
  var initPromise = null;

  function assetsHost(apiHost) {
    var host = (apiHost || DEFAULT_HOST).replace(/\/$/, '');
    return host.replace('.i.posthog.com', '-assets.i.posthog.com');
  }

  function ensureStub() {
    if (window.posthog && window.posthog.__loaded) return window.posthog;
    var ph = (window.posthog = window.posthog || []);
    if (ph.__stubbed) return ph;
    ph.__stubbed = true;
    if (!ph.init) {
      ph.init = function (token, config) {
        ph._i = ph._i || [];
        ph._i.push([token, config || {}, 'posthog']);
      };
    }
    [
      'capture',
      'identify',
      'alias',
      'people.set',
      'people.set_once',
      'reset',
      'group',
      'onFeatureFlags',
      'isFeatureEnabled',
      'getFeatureFlag',
      'register',
      'unregister',
      'setPersonProperties',
      'get_distinct_id',
      'get_session_id',
    ].forEach(function (method) {
      var parts = method.split('.');
      if (parts.length === 2) {
        ph[parts[0]] = ph[parts[0]] || {};
        ph[parts[0]][parts[1]] = function () {
          ph.push([method].concat([].slice.call(arguments)));
        };
      } else {
        ph[method] = function () {
          ph.push([method].concat([].slice.call(arguments)));
        };
      }
    });
    return ph;
  }

  function loadSnippet(apiHost) {
    return new Promise(function (resolve, reject) {
      if (window.posthog && window.posthog.__loaded) {
        resolve(window.posthog);
        return;
      }
      ensureStub();
      var script = document.createElement('script');
      script.async = true;
      script.crossOrigin = 'anonymous';
      script.src = assetsHost(apiHost) + '/static/array.js';
      script.onload = function () {
        resolve(window.posthog);
      };
      script.onerror = function () {
        reject(new Error('Failed to load PostHog'));
      };
      document.head.appendChild(script);
    });
  }

  function initPostHog(options) {
    options = options || {};
    if (initPromise) return initPromise;

    initPromise = (async function () {
      var cfg = options.config || null;
      if (!cfg) {
        try {
          cfg = await fetch('/api/config').then(function (r) {
            return r.json();
          });
        } catch (e) {
          return null;
        }
      }
      if (!cfg || !cfg.posthog_enabled || !cfg.posthog_key) return null;

      var host = cfg.posthog_host || DEFAULT_HOST;
      await loadSnippet(host);
      window.posthog.init(cfg.posthog_key, {
        api_host: host,
        ui_host: 'https://us.posthog.com',
        person_profiles: 'identified_only',
        capture_pageview: options.capturePageview !== false,
        capture_pageleave: true,
        persistence: 'localStorage+cookie',
        loaded: function (ph) {
          if (options.onLoaded) options.onLoaded(ph);
        },
      });
      return window.posthog;
    })().catch(function (err) {
      console.warn('PostHog init failed:', err);
      return null;
    });

    return initPromise;
  }

  function capture(event, properties) {
    if (!event) return;
    try {
      if (window.posthog && typeof window.posthog.capture === 'function') {
        window.posthog.capture(
          event,
          Object.assign({ source: 'frontend' }, properties || {})
        );
      }
    } catch (e) {}
  }

  function identify(userId, properties) {
    if (!userId) return;
    try {
      if (window.posthog && typeof window.posthog.identify === 'function') {
        window.posthog.identify(String(userId), properties || {});
      }
    } catch (e) {}
  }

  function reset() {
    try {
      if (window.posthog && typeof window.posthog.reset === 'function') {
        window.posthog.reset();
      }
    } catch (e) {}
  }

  window.FabplayAnalytics = {
    init: initPostHog,
    capture: capture,
    identify: identify,
    reset: reset,
  };
})(window, document);
