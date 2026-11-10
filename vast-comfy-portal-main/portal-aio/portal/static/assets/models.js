/*
 * Models page: download ComfyUI models on demand.
 * Backend: portal/model_manager.py (routes under /models).
 */
window.ModelManager = (function () {
    'use strict';

    const POLL_MS = 1500;
    const STATUS_REFRESH_MS = 10000;
    const ACTIVE = ['queued', 'downloading'];

    const $ = (id) => document.getElementById(id);
    const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

    function fmtBytes(n) {
        if (n === null || n === undefined) return '—';
        const units = ['B', 'KB', 'MB', 'GB', 'TB'];
        let v = n, i = 0;
        while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
        return i === 0 ? `${v} B` : `${v.toFixed(v >= 100 ? 0 : 1)} ${units[i]}`;
    }

    function fmtEta(sec) {
        if (!isFinite(sec) || sec <= 0) return '';
        if (sec < 60) return `${Math.round(sec)}s`;
        if (sec < 3600) return `${Math.floor(sec / 60)}m ${Math.round(sec % 60)}s`;
        return `${Math.floor(sec / 3600)}h ${Math.floor((sec % 3600) / 60)}m`;
    }

    function shortUrl(url) {
        try {
            const u = new URL(url);
            const path = decodeURIComponent(u.pathname);
            return u.host + (path.length > 60 ? path.slice(0, 28) + '…' + path.slice(-28) : path);
        } catch (e) {
            return url;
        }
    }

    function baseName(url) {
        try {
            return decodeURIComponent(new URL(url).pathname.split('/').filter(Boolean).pop() || url);
        } catch (e) {
            return url;
        }
    }

    async function api(path, options = {}) {
        const opts = { ...options };
        if (opts.body && typeof opts.body !== 'string') {
            opts.body = JSON.stringify(opts.body);
            opts.headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
        }
        const res = await fetch(`/models${path}`, opts);
        let data = null;
        try { data = await res.json(); } catch (e) { /* no body */ }
        if (!res.ok) throw new Error((data && data.detail) || `Request failed (${res.status})`);
        return data;
    }

    function toast(message, type = 'success') {
        if (window.app && window.app.showToast) window.app.showToast(esc(message), type);
    }

    const ICON_X = '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>';
    const ICON_DOWNLOAD = '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>';
    const ICON_TRASH = '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"></path><path d="M10 11v6"></path><path d="M14 11v6"></path></svg>';
    const ICON_RETRY = '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"></polyline><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"></path></svg>';

    // Badge label + .status-badge colour class (shared with the Supervisor page).
    const PROBE_BADGES = {
        checking: ['Checking', 'starting'],
        ok: ['Ready', 'running'],
        exists: ['Installed', 'stopped'],
        auth_required: ['Login needed', 'fatal'],
        not_found: ['Not found', 'fatal'],
        error: ['Error', 'fatal'],
    };
    const JOB_BADGES = {
        queued: ['Queued', 'starting'],
        downloading: ['Downloading', 'starting'],
        completed: ['Done', 'running'],
        skipped: ['Installed', 'stopped'],
        failed: ['Failed', 'fatal'],
        cancelled: ['Cancelled', 'stopped'],
        interrupted: ['Interrupted', 'fatal'],
    };
    const badge = ([label, cls]) => `<span class="status-badge ${cls}">${esc(label)}</span>`;

    return {
        status: null,
        jobs: [],
        list: { items: [], sources: [] },
        groups: { groups: [], sources: [] },
        groupOpen: new Set(),    // "<group id>:<section>" of the expanded LoRAs / Extras
        groupSel: new Set(),     // "<group id>|<section>|<url>" ticked for download
        sizes: new Map(),        // setup/extra URL -> probe result (size, access)
        installed: { folders: [], total_size: 0 },
        probes: new Map(),       // pasted URL -> probe result + the user's choices
        tab: 'list',
        listFilter: '',
        _jobStates: new Map(),
        _probeTimer: null,
        _lastStatus: 0,

        // -- lifecycle ------------------------------------------------------- //

        init: async function () {
            this.bind();
            await this.refreshStatus();
            const nav = document.querySelector('[data-page="models-page"]');
            if (nav && !(this.status && this.status.enabled)) nav.style.display = 'none';
            await this.refreshJobs();
            setInterval(() => this.tick(), POLL_MS);
        },

        visible: function () {
            const page = $('models-page');
            return !!page && page.style.display !== 'none';
        },

        show: async function () {
            await this.refreshStatus();
            if (!(this.status && this.status.enabled)) return;
            await Promise.all([this.refreshJobs(), this.refreshGroups(), this.refreshList(), this.refreshInstalled()]);
        },

        tick: async function () {
            const active = this.jobs.some(j => ACTIVE.includes(j.status));
            if (!active && !this.visible()) return;
            if (active || this.visible()) await this.refreshJobs();
            if (this.visible() && Date.now() - this._lastStatus > STATUS_REFRESH_MS) {
                await this.refreshStatus();
            }
        },

        // -- data ------------------------------------------------------------ //

        refreshStatus: async function () {
            try {
                this.status = await api('/status');
            } catch (e) {
                this.status = null;
            }
            this._lastStatus = Date.now();
            this.renderStatus();
        },

        refreshJobs: async function () {
            let jobs;
            try { jobs = await api('/jobs'); } catch (e) { return; }
            let finished = false;
            for (const job of jobs) {
                const prev = this._jobStates.get(job.id);
                if (prev && ACTIVE.includes(prev) && !ACTIVE.includes(job.status)) {
                    finished = true;
                    const label = job.filename || baseName(job.url);
                    if (job.status === 'completed') toast(`Downloaded ${label}`);
                    else if (job.status === 'failed') toast(`${label}: ${job.error}`, 'error');
                }
                this._jobStates.set(job.id, job.status);
            }
            this.jobs = jobs;
            this.renderJobs();
            if (finished) {
                await Promise.all([this.refreshGroups(), this.refreshList(), this.refreshInstalled(), this.refreshStatus()]);
            }
        },

        refreshList: async function () {
            try { this.list = await api('/list'); } catch (e) { toast(e.message, 'error'); }
            this.renderList();
        },

        refreshGroups: async function () {
            try { this.groups = await api('/groups'); } catch (e) { toast(e.message, 'error'); }
            this.renderGroups();
            this.probeGroupSizes();
        },

        // Setup and extra files only name a link: ask for their size (and whether
        // HF_TOKEN gives access) once. Folder listings already carry sizes.
        probeGroupSizes: async function () {
            const urls = [...new Set((this.groups.groups || [])
                .flatMap(g => [...g.setup, ...g.extras])
                .filter(i => !i.installed && i.size == null && !this.sizes.has(i.url))
                .map(i => i.url))].slice(0, 50);
            if (!urls.length) return;
            urls.forEach(u => this.sizes.set(u, null));
            let results;
            try {
                results = await api('/probe', { method: 'POST', body: { urls } });
            } catch (e) {
                urls.forEach(u => this.sizes.delete(u));
                return;
            }
            results.forEach(r => this.sizes.set(r.input, r));
            this.renderGroups();
        },

        itemSize: function (item) {
            if (item.size != null) return item.size;
            const probe = this.sizes.get(item.url);
            return probe ? probe.size : null;
        },

        itemProblem: function (item) {
            const probe = this.sizes.get(item.url);
            return probe && ['auth_required', 'not_found', 'error'].includes(probe.status) ? probe : null;
        },

        refreshInstalled: async function () {
            try { this.installed = await api('/installed'); } catch (e) { toast(e.message, 'error'); }
            this.renderInstalled();
        },

        // -- add models: paste, probe, download -------------------------------- //

        pastedUrls: function () {
            return [...new Set($('mm-urls').value.split(/\s+/).map(s => s.trim()).filter(Boolean))];
        },

        scheduleProbe: function () {
            clearTimeout(this._probeTimer);
            this._probeTimer = setTimeout(() => this.probeNow(), 600);
        },

        probeNow: async function () {
            const urls = this.pastedUrls();
            for (const key of [...this.probes.keys()]) {
                if (!urls.includes(key)) this.probes.delete(key);
            }
            const fresh = urls.filter(u => !this.probes.has(u));
            fresh.forEach(u => this.probes.set(u, { input: u, status: 'checking' }));
            this.renderPreview();
            if (!fresh.length) return;
            let results;
            try {
                results = await api('/probe', { method: 'POST', body: { urls: fresh } });
            } catch (e) {
                results = fresh.map(u => ({ input: u, status: 'error', message: e.message }));
            }
            for (const r of results) {
                if (!this.probes.has(r.input)) continue;  // removed while probing
                this.probes.set(r.input, { ...r, folderChoice: r.folder || '', filenameChoice: r.filename || '' });
            }
            this.renderPreview();
        },

        downloadableProbes: function () {
            return [...this.probes.values()].filter(p => p.status === 'ok' && !p.exists && p.folderChoice);
        },

        downloadPreview: async function () {
            const ready = this.downloadableProbes();
            if (!ready.length) return;
            const btn = $('mm-download-btn');
            btn.disabled = true;
            try {
                const res = await api('/download', {
                    method: 'POST',
                    body: {
                        items: ready.map(p => ({
                            url: p.url, folder: p.folderChoice,
                            filename: p.filenameChoice || null, name: p.name || null,
                        })),
                        save_to_list: $('mm-save').checked,
                    },
                });
                res.errors.forEach(e => toast(`${baseName(e.url)}: ${e.error}`, 'error'));
                if (res.jobs.length) toast(`${res.jobs.length} download${res.jobs.length > 1 ? 's' : ''} started`);
                const failed = new Set(res.errors.map(e => e.url));
                const started = ready.filter(p => !failed.has(p.url)).map(p => p.input);
                $('mm-urls').value = this.pastedUrls().filter(u => !started.includes(u)).join('\n');
                started.forEach(u => this.probes.delete(u));
                this.renderPreview();
                await Promise.all([this.refreshJobs(), this.refreshList()]);
            } catch (e) {
                toast(e.message, 'error');
            } finally {
                this.renderPreviewActions();
            }
        },

        // -- actions --------------------------------------------------------- //

        startDownloads: async function (items) {
            try {
                const res = await api('/download', { method: 'POST', body: { items, save_to_list: false } });
                res.errors.forEach(e => toast(`${baseName(e.url)}: ${e.error}`, 'error'));
                if (res.jobs.length) toast(`${res.jobs.length} download${res.jobs.length > 1 ? 's' : ''} started`);
            } catch (e) {
                toast(e.message, 'error');
            }
            await Promise.all([this.refreshJobs(), this.refreshGroups(), this.refreshList()]);
        },

        groupItems: function (gi, section, onlyKeys) {
            const g = (this.groups.groups || [])[gi];
            if (!g) return [];
            return g[section].filter(i => !i.installed && !i.job_id &&
                (!onlyKeys || this.groupSel.has(`${g.id}|${section}|${i.url}`)));
        },

        downloadGroupItems: function (gi, section, onlyKeys) {
            const g = (this.groups.groups || [])[gi];
            const items = this.groupItems(gi, section, onlyKeys);
            if (!g || !items.length) return;
            items.forEach(i => this.groupSel.delete(`${g.id}|${section}|${i.url}`));
            this.startDownloads(items.map(i => ({
                url: i.url, folder: i.folder, filename: i.filename || null, name: i.name || null,
            })));
        },

        jobAction: async function (id, action) {
            try {
                await api(`/jobs/${encodeURIComponent(id)}/${action}`, { method: 'POST' });
            } catch (e) {
                toast(e.message, 'error');
            }
            await this.refreshJobs();
        },

        removeFromList: async function (index) {
            const item = this.list.items[index];
            if (!item) return;
            try {
                await api('/list/remove', { method: 'POST', body: { url: item.url, folder: item.folder } });
            } catch (e) {
                toast(e.message, 'error');
            }
            await this.refreshList();
        },

        deleteFile: async function (folder, path) {
            if (!confirm(`Delete ${folder}/${path} from the instance?`)) return;
            try {
                await api('/installed/delete', { method: 'POST', body: { folder, path } });
                toast(`Deleted ${path}`);
            } catch (e) {
                toast(e.message, 'error');
            }
            await Promise.all([this.refreshInstalled(), this.refreshGroups(), this.refreshList(), this.refreshStatus()]);
        },

        setTab: function (tab) {
            this.tab = tab;
            document.querySelectorAll('#models-page .mm-tab').forEach(b =>
                b.classList.toggle('active', b.dataset.tab === tab));
            $('mm-list-panel').hidden = tab !== 'list';
            $('mm-installed-panel').hidden = tab !== 'installed';
        },

        bind: function () {
            const page = $('models-page');
            if (!page) return;
            $('mm-urls').addEventListener('input', () => this.scheduleProbe());
            $('mm-urls').addEventListener('paste', () => setTimeout(() => this.probeNow(), 0));

            // <details> toggle events don't bubble: listen in the capture phase.
            page.addEventListener('toggle', (e) => {
                const key = e.target.dataset && e.target.dataset.section;
                if (!key) return;
                if (e.target.open) this.groupOpen.add(key); else this.groupOpen.delete(key);
            }, true);

            page.addEventListener('change', (e) => {
                if (e.target.dataset.sel) {
                    if (e.target.checked) this.groupSel.add(e.target.dataset.sel);
                    else this.groupSel.delete(e.target.dataset.sel);
                    this.renderGroups();
                    return;
                }
                const row = e.target.closest('[data-probe]');
                if (row) {
                    const probe = this.probes.get(row.dataset.probe);
                    if (!probe) return;
                    if (e.target.dataset.field === 'folder') probe.folderChoice = e.target.value;
                    if (e.target.dataset.field === 'filename') probe.filenameChoice = e.target.value.trim();
                    this.renderPreviewActions();
                }
            });

            page.addEventListener('input', (e) => {
                if (e.target.id === 'mm-list-filter') {
                    this.listFilter = e.target.value.toLowerCase();
                    this.renderListRows();
                }
            });

            page.addEventListener('click', (e) => {
                const el = e.target.closest('[data-action]');
                if (!el) return;
                const d = el.dataset;
                switch (d.action) {
                    case 'download-preview': this.downloadPreview(); break;
                    case 'remove-line': {
                        $('mm-urls').value = this.pastedUrls().filter(u => u !== d.url).join('\n');
                        this.probes.delete(d.url);
                        this.renderPreview();
                        break;
                    }
                    case 'cancel-job': this.jobAction(d.id, 'cancel'); break;
                    case 'retry-job': this.jobAction(d.id, 'retry'); break;
                    case 'clear-jobs':
                        api('/jobs/clear', { method: 'POST' }).then(() => this.refreshJobs());
                        break;
                    case 'tab': this.setTab(d.tab); break;
                    case 'download-item': {
                        const item = this.list.items[+d.index];
                        if (item) this.startDownloads([{ url: item.url, folder: item.folder, filename: item.filename, name: item.name }]);
                        break;
                    }
                    case 'download-missing': {
                        const missing = this.list.items.filter(i => !i.installed && !i.job_id);
                        this.startDownloads(missing.map(i => ({ url: i.url, folder: i.folder, filename: i.filename, name: i.name })));
                        break;
                    }
                    case 'install-setup': this.downloadGroupItems(+d.group, 'setup', false); break;
                    case 'download-selected': this.downloadGroupItems(+d.group, d.section, true); break;
                    case 'download-character': {
                        const g = this.groups.groups[+d.group];
                        const c = g && g.characters[+d.char];
                        if (!c) break;
                        this.startDownloads(c.loras.filter(i => !i.installed && !i.job_id).map(i => ({
                            url: i.url, folder: i.folder, filename: i.filename || null, name: i.name || null,
                        })));
                        break;
                    }
                    case 'select-missing': {
                        const g = this.groups.groups[+d.group];
                        const missing = this.groupItems(+d.group, d.section, false);
                        const keys = missing.map(i => `${g.id}|${d.section}|${i.url}`);
                        const all = keys.every(k => this.groupSel.has(k));
                        keys.forEach(k => all ? this.groupSel.delete(k) : this.groupSel.add(k));
                        this.renderGroups();
                        break;
                    }
                    case 'remove-item': this.removeFromList(+d.index); break;
                    case 'delete-file': this.deleteFile(d.folder, d.path); break;
                }
            });
        },

        // -- rendering ------------------------------------------------------- //

        renderStatus: function () {
            const bar = $('mm-statusbar');
            if (!bar) return;
            const s = this.status;
            const enabled = !!(s && s.enabled);
            $('mm-content').hidden = !enabled;
            const unavailable = $('mm-unavailable');
            unavailable.hidden = enabled;
            if (!enabled) {
                bar.innerHTML = '';
                unavailable.innerHTML = s
                    ? `<h2>No ComfyUI models folder</h2><p>Nothing found at <code>${esc(s.models_dir)}</code>. ComfyUI may still be installing; otherwise set <code>MODEL_MANAGER_DIR</code> to its models folder.</p>`
                    : '<h2>Model manager unavailable</h2><p>The portal did not answer. Try reloading the page.</p>';
                return;
            }
            const token = (ok, name) => `<span class="mm-pill ${ok ? 'ok' : 'off'}" title="${ok ? `${name} is set` : `Set ${name} in your Vast account to download gated or private files`}">${esc(name)} ${ok ? '✓' : 'not set'}</span>`;
            const disk = s.disk;
            const pct = disk ? Math.round((disk.used / disk.total) * 100) : 0;
            bar.innerHTML = `
                <span class="mm-pill mm-path" title="Models folder: ${esc(s.models_dir)}"><code>${esc(s.models_dir)}</code></span>
                ${disk ? `<span class="mm-pill ${pct > 90 ? 'warn' : ''}">${fmtBytes(disk.free)} free of ${fmtBytes(disk.total)}</span>` : ''}
                ${token(s.tokens.hf, 'HF_TOKEN')}
                ${token(s.tokens.civitai, 'CIVITAI_TOKEN')}
                <span class="mm-hint mm-statusbar-hint">New files show up in ComfyUI after a refresh (press <kbd>R</kbd>).</span>`;
        },

        folderOptions: function (selected) {
            const folders = (this.status && this.status.folders) || [];
            const opts = folders.map(f => `<option value="${esc(f)}" ${f === selected ? 'selected' : ''}>${esc(f)}</option>`);
            return `<option value="" ${selected ? '' : 'selected'} disabled>Choose folder…</option>${opts.join('')}`;
        },

        renderPreview: function () {
            const box = $('mm-preview');
            const probes = [...this.probes.values()];
            box.innerHTML = probes.map(p => {
                const key = p.exists ? 'exists' : p.status;
                const checking = p.status === 'checking';
                const title = p.name || p.filename || baseName(p.input);
                const sub = p.message ? `<span class="mm-sub-msg">${esc(p.message)}</span>` : esc(shortUrl(p.url || p.input));
                const editable = !checking && p.status !== 'error' && p.status !== 'not_found';
                return `<div class="mm-row mm-preview-row" data-probe="${esc(p.input)}">
                    <div class="mm-main">
                        <div class="mm-name" title="${esc(p.url || p.input)}">${esc(title)}</div>
                        <div class="mm-sub">${sub}</div>
                    </div>
                    <input class="mm-input" data-field="filename" placeholder="file name" value="${esc(p.filenameChoice || '')}" ${editable ? '' : 'disabled'} title="Saved as">
                    <select class="mm-select ${editable && !p.folderChoice ? 'needs-choice' : ''}" data-field="folder" ${editable ? '' : 'disabled'}>${this.folderOptions(p.folderChoice)}</select>
                    <div class="mm-size">${checking ? '' : fmtBytes(p.size)}</div>
                    <div>${badge(PROBE_BADGES[key] || PROBE_BADGES.error)}</div>
                    <button class="mm-icon-btn" data-action="remove-line" data-url="${esc(p.input)}" title="Remove">${ICON_X}</button>
                </div>`;
            }).join('');
            this.renderPreviewActions();
        },

        renderPreviewActions: function () {
            const ready = this.downloadableProbes();
            const btn = $('mm-download-btn');
            const total = ready.reduce((sum, p) => sum + (p.size || 0), 0);
            btn.disabled = ready.length === 0;
            btn.textContent = ready.length
                ? `Download ${ready.length} file${ready.length > 1 ? 's' : ''}${total ? ` · ${fmtBytes(total)}` : ''}`
                : 'Download';
            const probes = [...this.probes.values()];
            const needFolder = probes.filter(p => p.status === 'ok' && !p.exists && !p.folderChoice).length;
            const checking = probes.filter(p => p.status === 'checking').length;
            const free = this.status && this.status.disk ? this.status.disk.free : null;
            let hint = '';
            if (checking) hint = `Checking ${checking} link${checking > 1 ? 's' : ''}…`;
            else if (needFolder) hint = `Pick a folder for ${needFolder} link${needFolder > 1 ? 's' : ''}`;
            else if (free !== null && total > free) hint = `Not enough free disk space (${fmtBytes(free)} free)`;
            $('mm-preview-hint').textContent = hint;
        },

        jobDetail: function (j) {
            switch (j.status) {
                case 'downloading': {
                    const parts = [`${fmtBytes(j.downloaded)} / ${fmtBytes(j.total)}`];
                    if (j.speed) parts.push(`${fmtBytes(j.speed)}/s`);
                    const eta = j.total && j.speed ? fmtEta((j.total - j.downloaded) / j.speed) : '';
                    if (eta) parts.push(`${eta} left`);
                    return parts.join(' · ');
                }
                case 'queued': return 'Waiting for a free slot';
                case 'completed': return `${fmtBytes(j.total || j.downloaded)} downloaded`;
                case 'skipped': return 'Already on disk';
                case 'cancelled': return 'Cancelled';
                default: return j.error || '';
            }
        },

        renderJobs: function () {
            const panel = $('mm-jobs-panel');
            const box = $('mm-jobs');
            panel.hidden = this.jobs.length === 0;
            const ids = new Set(this.jobs.map(j => j.id));
            box.querySelectorAll('[data-job]').forEach(el => { if (!ids.has(el.dataset.job)) el.remove(); });

            this.jobs.forEach((j, index) => {
                const active = ACTIVE.includes(j.status);
                const pct = j.total ? Math.min(100, (j.downloaded / j.total) * 100) : (j.status === 'completed' ? 100 : 0);
                const button = active
                    ? `<button class="service-btn danger" data-action="cancel-job" data-id="${esc(j.id)}">${ICON_X} Cancel</button>`
                    : (['failed', 'cancelled', 'interrupted'].includes(j.status)
                        ? `<button class="service-btn" data-action="retry-job" data-id="${esc(j.id)}">${ICON_RETRY} Retry</button>` : '');
                // Rebuild a row only when its status changes, so buttons stay clickable between polls.
                const sig = `${j.status}|${j.filename}|${j.error}`;
                let el = box.querySelector(`[data-job="${CSS.escape(j.id)}"]`);
                if (!el || el.dataset.sig !== sig) {
                    const html = `<div class="mm-job" data-job="${esc(j.id)}" data-sig="${esc(sig)}">
                        <div class="mm-row mm-job-row">
                            <div class="mm-main">
                                <div class="mm-name" title="${esc(j.url)}">${esc(j.filename || j.name || baseName(j.url))}</div>
                                <div class="mm-sub"><span class="mm-chip">${esc(j.folder)}</span> <span class="mm-job-detail ${j.status === 'failed' ? 'mm-sub-msg' : ''}"></span></div>
                            </div>
                            <div>${badge(JOB_BADGES[j.status] || JOB_BADGES.failed)}</div>
                            <div class="mm-row-actions">${button}</div>
                        </div>
                        ${active || j.status === 'completed' ? '<div class="mm-progress"><div class="mm-progress-fill"></div></div>' : ''}
                    </div>`;
                    const tpl = document.createElement('template');
                    tpl.innerHTML = html.trim();
                    const fresh = tpl.content.firstChild;
                    if (el) el.replaceWith(fresh); else box.appendChild(fresh);
                    el = fresh;
                }
                el.querySelector('.mm-job-detail').textContent = this.jobDetail(j);
                const fill = el.querySelector('.mm-progress-fill');
                if (fill) {
                    fill.style.width = `${pct}%`;
                    fill.classList.toggle('indeterminate', j.status === 'downloading' && !j.total);
                }
                if (box.children[index] !== el) box.insertBefore(el, box.children[index] || null);
            });
        },

        renderGroups: function () {
            const panel = $('mm-groups-panel');
            if (!panel) return;
            const groups = this.groups.groups || [];
            const failed = (this.groups.sources || []).filter(s => !s.ok);
            panel.hidden = !groups.length && !failed.length;
            if (panel.hidden) return;
            panel.innerHTML = `
                <div class="mm-panel-header">
                    <h2>Setups</h2>
                    <span class="mm-hint">From MODEL_CATALOG &mdash; files shared by several setups download once</span>
                </div>
                ${failed.map(s => `<div class="mm-warning">Could not load MODEL_CATALOG source <code>${esc(s.source)}</code>: ${esc(s.error)}</div>`).join('')}
                ${(this.groups.errors || []).map(err => `<div class="mm-warning">${esc(err)}</div>`).join('')}
                <div class="mm-groups">${groups.map((g, gi) => this.groupCard(g, gi)).join('')}</div>`;
        },

        sumSizes: function (items) {
            const sizes = items.map(i => this.itemSize(i));
            const known = sizes.filter(s => s != null);
            return known.length ? known.reduce((a, b) => a + b, 0) : null;
        },

        groupCard: function (g, gi) {
            const setup = g.setup;
            const done = setup.filter(i => i.installed).length;
            const busy = setup.some(i => i.job_id);
            const missing = this.groupItems(gi, 'setup', false);
            const size = this.sumSizes(missing);
            let state = '';
            if (setup.length) {
                state = done === setup.length ? badge(['Installed', 'running'])
                    : busy ? badge(['Downloading', 'starting'])
                    : badge([`${done}/${setup.length} installed`, 'stopped']);
            }
            const free = this.status && this.status.disk ? this.status.disk.free : null;
            const noRoom = size && free !== null && size > free
                ? `<span class="mm-pill warn">Not enough disk space: ${fmtBytes(free)} free</span>` : '';
            const install = missing.length
                ? `${noRoom}<button class="tunnel-btn mm-install-btn" data-action="install-setup" data-group="${gi}">${ICON_DOWNLOAD} Install setup · ${missing.length} file${missing.length > 1 ? 's' : ''}${size ? ` · ${fmtBytes(size)}` : ''}</button>`
                : '';
            return `<div class="mm-group">
                <div class="mm-group-head">
                    <div class="mm-main">
                        <h3>${esc(g.name)}</h3>
                        ${g.description ? `<div class="mm-sub">${esc(g.description)}</div>` : ''}
                    </div>
                    ${state}
                    ${install}
                </div>
                ${g.errors.map(err => `<div class="mm-warning">${esc(err)}</div>`).join('')}
                ${setup.length ? `<div class="mm-group-setup">${setup.map(i => this.groupRow(g, 'setup', i, false)).join('')}</div>` : ''}
                ${this.characterStrip(g, gi)}
                ${this.groupSection(g, gi, 'loras', 'LoRAs')}
                ${this.groupSection(g, gi, 'extras', 'Other models')}
            </div>`;
        },

        characterStrip: function (g, gi) {
            const chars = g.characters || [];
            if (!chars.length) return '';
            return `<div class="mm-group-chars">
                <div class="mm-chars-title">Characters</div>
                <div class="mm-chars">${chars.map((c, ci) => this.characterCard(c, gi, ci)).join('')}</div>
            </div>`;
        },

        characterCard: function (c, gi, ci) {
            const files = c.loras;
            const done = files.filter(i => i.installed).length;
            const busy = files.some(i => i.job_id);
            const missing = files.filter(i => !i.installed && !i.job_id);
            const size = this.sumSizes(missing);
            const picture = c.image
                ? `<img class="mm-char-img" src="${esc(c.image)}" alt="${esc(c.name)}" loading="lazy">`
                : `<div class="mm-char-img mm-char-initial">${esc(c.name.charAt(0).toUpperCase())}</div>`;
            let action;
            if (done === files.length) action = badge(['Installed', 'running']);
            else if (busy) action = badge(['Downloading', 'starting']);
            else action = `<button class="service-btn" data-action="download-character" data-group="${gi}" data-char="${ci}">${ICON_DOWNLOAD} ${done ? `${missing.length} more` : 'Download'}${size ? ` · ${fmtBytes(size)}` : ''}</button>`;
            const names = files.map(i => `${i.installed ? '✓ ' : ''}${i.filename}`).join('\n');
            return `<div class="mm-char ${done === files.length ? 'installed' : ''}" title="${esc(names)}">
                ${picture}
                <div class="mm-char-name">${esc(c.name)}</div>
                <div class="mm-hint">${files.length} LoRA${files.length > 1 ? 's' : ''}</div>
                <div class="mm-char-action">${action}</div>
            </div>`;
        },

        groupSection: function (g, gi, section, label) {
            const items = g[section];
            if (!items.length) return '';
            const key = `${g.id}:${section}`;
            const installed = items.filter(i => i.installed).length;
            const missing = this.groupItems(gi, section, false);
            const picked = this.groupItems(gi, section, true);
            const size = this.sumSizes(picked);
            const allPicked = missing.length && picked.length === missing.length;
            return `<details class="mm-group-section" data-section="${esc(key)}" ${this.groupOpen.has(key) ? 'open' : ''}>
                <summary>${esc(label)} <span class="mm-count">${items.length}</span>${installed ? ` <span class="mm-hint">${installed} installed</span>` : ''}</summary>
                ${missing.length ? `<div class="mm-section-actions">
                    <button class="secondary-btn" data-action="select-missing" data-group="${gi}" data-section="${section}">${allPicked ? 'Clear selection' : 'Select all missing'}</button>
                    <button class="service-btn" data-action="download-selected" data-group="${gi}" data-section="${section}" ${picked.length ? '' : 'disabled'}>${ICON_DOWNLOAD} Download${picked.length ? ` ${picked.length}${size ? ` · ${fmtBytes(size)}` : ''}` : ' selected'}</button>
                </div>` : ''}
                ${items.map(i => this.groupRow(g, section, i, true)).join('')}
            </details>`;
        },

        groupRow: function (g, section, item, selectable) {
            const title = item.name || item.filename || baseName(item.url);
            const problem = !item.installed && this.itemProblem(item);
            const state = item.installed ? ['Installed', 'running']
                : item.job_id ? ['Downloading', 'starting']
                : problem ? PROBE_BADGES[problem.status]
                : ['Not downloaded', 'stopped'];
            const key = `${g.id}|${section}|${item.url}`;
            const check = !selectable ? ''
                : (!item.installed && !item.job_id
                    ? `<input type="checkbox" class="mm-row-check" data-sel="${esc(key)}" ${this.groupSel.has(key) ? 'checked' : ''} title="Select">`
                    : '<span></span>');
            return `<div class="mm-row mm-group-row ${selectable ? 'selectable' : ''}">
                ${check}
                <div class="mm-main">
                    <div class="mm-name" title="${esc(item.url)}">${esc(title)}</div>
                    <div class="mm-sub">${problem ? `<span class="mm-sub-msg">${esc(problem.message)}</span>` : esc(shortUrl(item.url))}</div>
                </div>
                <span class="mm-chip">${esc(item.folder)}</span>
                <div class="mm-size">${fmtBytes(this.itemSize(item))}</div>
                <div>${badge(state)}</div>
            </div>`;
        },

        renderList: function () {
            const panel = $('mm-list-panel');
            const items = this.list.items || [];
            $('mm-list-count').textContent = items.length ? items.length : '';
            const missing = items.filter(i => !i.installed && !i.job_id).length;
            const failedSources = (this.list.sources || []).filter(s => !s.ok);
            panel.innerHTML = `
                <div class="mm-panel-header">
                    <input id="mm-list-filter" class="mm-input mm-filter" placeholder="Filter…" value="${esc(this.listFilter)}" ${items.length ? '' : 'hidden'}>
                    <div class="mm-header-actions">
                        <button class="secondary-btn" data-action="download-missing" ${missing ? '' : 'disabled'}>${ICON_DOWNLOAD} Download missing${missing ? ` (${missing})` : ''}</button>
                        <a class="secondary-btn" href="/models/list/export" download="model-list.yaml" ${items.length ? '' : 'hidden'}>Export YAML</a>
                    </div>
                </div>
                ${failedSources.map(s => `<div class="mm-warning">Could not load MODEL_CATALOG source <code>${esc(s.source)}</code>: ${esc(s.error)}</div>`).join('')}
                <div id="mm-list-rows"></div>`;
            this.renderListRows();
        },

        renderListRows: function () {
            const box = $('mm-list-rows');
            if (!box) return;
            const items = this.list.items || [];
            if (!items.length) {
                box.innerHTML = `<div class="mm-empty-state">Your list is empty. Paste links above with <strong>Save to My list</strong> checked,
                    or set <code>MODEL_CATALOG</code> to the URL of a YAML list (use <em>Export YAML</em> to make one).</div>`;
                return;
            }
            const f = this.listFilter;
            const rows = items.map((item, index) => ({ item, index })).filter(({ item }) =>
                !f || [item.name, item.filename, item.url, item.folder].some(v => v && v.toLowerCase().includes(f)));
            box.innerHTML = rows.map(({ item, index }) => {
                const title = item.name || item.filename || baseName(item.url);
                const state = item.installed ? ['Installed', 'running'] : item.job_id ? ['Downloading', 'starting'] : ['Not downloaded', 'stopped'];
                const download = !item.installed && !item.job_id
                    ? `<button class="service-btn" data-action="download-item" data-index="${index}">${ICON_DOWNLOAD} Download</button>` : '';
                const remove = item.source === 'local'
                    ? `<button class="mm-icon-btn" data-action="remove-item" data-index="${index}" title="Remove from list">${ICON_X}</button>`
                    : '<span class="mm-icon-placeholder" title="From MODEL_CATALOG (read-only)"></span>';
                return `<div class="mm-row mm-list-row">
                    <div class="mm-main">
                        <div class="mm-name" title="${esc(item.url)}">${esc(title)}</div>
                        <div class="mm-sub">${esc(shortUrl(item.url))}${item.source !== 'local' ? ' · <em>catalog</em>' : ''}</div>
                    </div>
                    <span class="mm-chip">${esc(item.folder)}</span>
                    <div>${badge(state)}</div>
                    <div class="mm-row-actions">${download}${remove}</div>
                </div>`;
            }).join('') || '<div class="mm-empty-state">No match.</div>';
        },

        renderInstalled: function () {
            const panel = $('mm-installed-panel');
            const folders = (this.installed.folders || []).filter(f => f.files.length);
            const count = folders.reduce((n, f) => n + f.files.length, 0);
            $('mm-installed-count').textContent = count || '';
            if (!count) {
                panel.innerHTML = '<div class="mm-empty-state">No model files yet.</div>';
                return;
            }
            panel.innerHTML = `<div class="mm-summary">${count} file${count > 1 ? 's' : ''} · ${fmtBytes(this.installed.total_size)}</div>` +
                folders.map(f => `
                <div class="mm-folder">
                    <div class="mm-folder-head"><span class="mm-chip">${esc(f.folder)}</span><span class="mm-hint">${f.files.length} · ${fmtBytes(f.size)}</span></div>
                    ${f.files.map(file => `<div class="mm-row mm-file-row">
                        <div class="mm-main"><div class="mm-name" title="${esc(file.path)}">${esc(file.path)}</div></div>
                        <div class="mm-tags">${file.partial ? '<span class="mm-pill warn">incomplete</span>' : ''}${file.link ? '<span class="mm-pill" title="Symlink: deleting removes only the link">link</span>' : ''}</div>
                        <div class="mm-size">${fmtBytes(file.size)}</div>
                        <button class="mm-icon-btn danger" data-action="delete-file" data-folder="${esc(f.folder)}" data-path="${esc(file.path)}" title="Delete">${ICON_TRASH}</button>
                    </div>`).join('')}
                </div>`).join('');
        },
    };
})();

document.addEventListener('DOMContentLoaded', () => {
    window.ModelManager.init().catch(e => console.error('model manager init failed:', e));
});
