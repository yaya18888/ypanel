/* YPanel UI — Alpine.js single-page app */
async function api(method, url, body, isForm) {
  const opt = { method, headers: { 'X-YPanel': '1' }, credentials: 'same-origin' };
  if (body !== undefined) {
    if (isForm) opt.body = body;
    else { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
  }
  const r = await fetch(url, opt);
  const ct = r.headers.get('content-type') || '';
  const data = ct.includes('json') ? await r.json() : await r.text();
  if (r.status === 401 && url !== '/api/login' && window.__yp) window.__yp.authed = false;
  if (!r.ok) {
    let d = data && data.detail;
    if (Array.isArray(d)) d = d.map(e => (e.loc ? e.loc.slice(-1)[0] + ': ' : '') + e.msg).join('\n');
    throw new Error(d || ('HTTP ' + r.status));
  }
  return data;
}
const fmtBytes = b => {
  if (b === null || b === undefined) return '—';
  const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0; b = Number(b);
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return (i ? b.toFixed(1) : b) + ' ' + u[i];
};
const fmtDate = ts => ts ? new Date(ts * 1000).toLocaleString() : '—';
const fmtUptime = s => { const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60); return (d ? d + 'd ' : '') + h + 'h ' + m + 'm'; };
const isArchive = n => /\.(zip|tar|tgz|tar\.gz|tar\.bz2|tar\.xz|gz)$/i.test(n);

function panel() {
  return {
    authed: false, me: '', page: 'dashboard', menuOpen: false, busy: 0, toasts: [],
    titles: { dashboard: 'Dashboard', sites: 'Websites', site: 'Website', databases: 'Databases', files: 'File Manager',
      cron: 'Cron Jobs', backups: 'Backups', installers: 'Installers', settings: 'Settings', dns: 'DNS · Cloudflare', firewall: 'Firewall',
      mail: 'Email', users: 'Users' },
    role: 'admin', get isAdmin() { return this.role === 'admin'; },
    adminPages: ['dashboard', 'dns', 'firewall', 'mail', 'users'],
    // v3: users
    users: [], userForm: null, userCreds: null,
    // v3: redis + stats
    rcache: null, rserver: null, tstats: null, statsDays: 7,
    // v3: mail
    mail: { installed: false, setup: '', services: {}, counts: {} }, mailHost: '', mailChecks: null, mailTab: 'domains',
    mdomains: [], mboxes: [], maliases: [], mqueue: [], mdns: null, mdnsDomain: null, mailCreds: null,
    mdomForm: { domain: '', site_id: '' }, mboxForm: { domain_id: '', local: '', password: '', quota_mb: 1024 },
    maliasForm: { domain_id: '', local: '', destinations: '' }, mailTimer: null,
    login: { username: 'admin', password: '', code: '' }, need2fa: false,
    // v2: sftp
    sftp: null, sftpPw: '', sftpKeys: '', sftpCreds: null,
    // v2: dns
    zones: [], zone: null, records: [], recFilter: '', recForm: null, zoneSet: {}, purgeUrls: '',
    recTypes: ['A', 'AAAA', 'CNAME', 'TXT', 'MX', 'NS', 'SRV', 'CAA'],
    // v2: firewall
    fw: { active: false, available: true, rules: [], protected: [], jails: {} },
    fwForm: { action: 'allow', port: '', proto: 'tcp', source: '' }, blockIp: '',
    // v2: security + remote
    tfa: { enabled: false, recovery_left: 0 }, tfaSetup: null, tfaCode: '', tfaCodes: null, tfaPw: '',
    remoteForm: { enabled: false, provider: 'Cloudflare', endpoint: '', region: 'auto', bucket: '', access_key: '', secret: '', prefix: 'ypanel', keep_days: 30 },
    remoteFiles: null,
    // dashboard
    stats: null, services: [], procs: [], activity: [],
    // sites
    sites: [], phpVersions: [], showSiteForm: false,
    siteForm: { domain: '', www: true, php: '', cloudflare_dns: false, cf_proxied: true, ssl: false, ssl_method: 'http' },
    site: null, siteTab: 'overview', sslMethod: 'http', ini: {}, nginxCustom: '', vhostPreview: '', logKind: 'error', logText: '', usage: null,
    del: { files: true, dbs: true },
    // databases
    dbs: [], dbForm: { site_id: '', name: '' }, dbCreds: null,
    // files
    fm: { site_id: '', path: 'public_html', items: [], sel: [], truncated: false },
    editor: null,
    // cron
    crons: [], cronSite: '', cronForm: { site_id: '', schedule: '*/5 * * * *', command: '' },
    cronPresets: [['Every minute', '* * * * *'], ['Every 5 minutes', '*/5 * * * *'], ['Every 15 minutes', '*/15 * * * *'],
      ['Hourly', '0 * * * *'], ['Daily 03:00', '0 3 * * *'], ['Weekly (Sun)', '0 4 * * 0'], ['Monthly', '0 5 1 * *']],
    // backups
    backups: [], bkSite: '', bkTimer: null,
    // installers
    wp: { site_id: '', title: '', admin_user: 'admin', admin_password: '', admin_email: '', locale: 'en_US', force: false },
    git: { site_id: '', repo: '', branch: '', force: false },
    clone: { source_id: '', target_id: '', exclude: '', force: false },
    installResult: null,
    // settings
    settings: {}, cfToken: '', cfInfo: null, pw: { current: '', new: '' },

    fmtBytes, fmtDate, fmtUptime, isArchive,

    async init() {
      window.__yp = this;
      try { const m = await api('GET', '/api/me'); this.me = m.username; this.role = m.role; this.authed = true; this.go(this.home()); } catch (e) { }
      setInterval(() => { if (this.authed && this.page === 'dashboard' && !document.hidden) this.loadDash(true); }, 8000);
    },
    toast(msg, type = 'ok') {
      const id = Math.random(); this.toasts.push({ id, msg, type });
      setTimeout(() => this.toasts = this.toasts.filter(t => t.id !== id), type === 'err' ? 9000 : 3500);
    },
    async call(fn, okMsg) {
      this.busy++;
      try { const r = await fn(); if (okMsg) this.toast(okMsg); return r; }
      catch (e) { this.toast(e.message, 'err'); return undefined; }
      finally { this.busy--; }
    },
    async doLogin() {
      this.busy++;
      try {
        const r = await api('POST', '/api/login', this.login);
        this.me = r.username; this.role = r.role || 'admin'; this.authed = true; this.login.password = ''; this.login.code = ''; this.need2fa = false;
        this.go(this.home());
      } catch (e) {
        if (e.message === '2FA_REQUIRED') { this.need2fa = true; this.$nextTick(() => document.getElementById('code2fa')?.focus()); }
        else { this.toast(e.message, 'err'); if (this.need2fa) this.login.code = ''; }
      } finally { this.busy--; }
    },
    async logout() { await api('POST', '/api/logout').catch(() => { }); this.authed = false; },
    home() { return this.isAdmin ? 'dashboard' : 'sites'; },
    go(p) {
      if (!this.isAdmin && this.adminPages.includes(p)) p = 'sites';
      if (this.mailTimer && p !== 'mail') { clearInterval(this.mailTimer); this.mailTimer = null; }
      this.page = p; this.menuOpen = false; window.scrollTo(0, 0);
      if (this.bkTimer && p !== 'backups') { clearInterval(this.bkTimer); this.bkTimer = null; }
      const loaders = {
        dashboard: () => this.loadDash(), sites: () => this.loadSites(), databases: () => { this.loadSites(); this.loadDbs(); },
        files: () => this.loadSites().then(() => { if (!this.fm.site_id && this.sites[0]) { this.fm.site_id = this.sites[0].id; this.fm.path = 'public_html'; } if (this.fm.site_id) this.fmList(); }),
        cron: () => { this.loadSites(); this.loadCrons(); }, backups: () => { this.loadSites(); this.loadBackups(); this.remoteFiles = null; if (this.isAdmin && !this.settings.version) this.loadSettings(); },
        installers: () => this.loadSites(),
        settings: () => { if (this.isAdmin) { this.loadSettings(); this.loadRedisServer(); } this.loadTfa(); },
        users: () => { this.loadSites(); this.loadUsers(); }, mail: () => { this.loadSites(); this.loadMail(); },
        dns: () => { this.loadZones(); if (!this.settings.version) this.loadSettings(); }, firewall: () => this.loadFw(),
      };
      loaders[p] && loaders[p]();
    },

    // ---------- dashboard ----------
    async loadDash(silent) {
      const f = async () => {
        const [s, sv, pr, ac] = await Promise.all([api('GET', '/api/server/stats'), api('GET', '/api/server/services'),
          api('GET', '/api/server/processes'), api('GET', '/api/server/activity?limit=15')]);
        this.stats = s; this.services = sv; this.procs = pr; this.activity = ac;
      };
      silent ? f().catch(() => { }) : this.call(f);
    },
    barClass(p) { return p > 90 ? 'crit' : p > 75 ? 'hot' : ''; },
    async svc(name, action) {
      if (action === 'stop' && !confirm(`Stop ${name}?`)) return;
      await this.call(() => api('POST', `/api/server/services/${name}/${action}`), `${name}: ${action} ok`);
      this.loadDash(true);
    },
    async killProc(p) {
      if (!confirm(`Terminate ${p.name} (PID ${p.pid})?`)) return;
      await this.call(() => api('POST', '/api/server/kill', { pid: p.pid }), 'Process terminated');
      this.loadDash(true);
    },

    // ---------- sites ----------
    async loadSites() {
      await this.call(async () => {
        this.sites = await api('GET', '/api/sites');
        if (!this.isAdmin) return;
        const php = await api('GET', '/api/server/php');
        this.phpVersions = php.versions;
        if (!this.siteForm.php) this.siteForm.php = php.default || php.versions[php.versions.length - 1] || '';
      });
    },
    siteName(id) { const s = this.sites.find(x => x.id == id); return s ? s.domain : '?'; },
    async createSite() {
      const r = await this.call(() => api('POST', '/api/sites', this.siteForm));
      if (!r) return;
      this.toast(`${r.domain} created (user ${r.user})`);
      (r.warnings || []).forEach(w => this.toast(w, 'err'));
      this.showSiteForm = false; this.siteForm.domain = '';
      await this.loadSites(); this.openSite(r.id);
    },
    async openSite(id, tab) {
      const s = await this.call(() => api('GET', `/api/sites/${id}`));
      if (!s) return;
      this.site = s; this.ini = { ...s.ini }; this.siteTab = tab || 'overview'; this.usage = null; this.logText = ''; this.sftp = null; this.sftpCreds = null; this.rcache = null; this.tstats = null;
      this.page = 'site'; this.menuOpen = false; window.scrollTo(0, 0);
    },
    async reloadSite() { if (this.site) await this.openSite(this.site.id, this.siteTab); },
    async siteTabOpen(t) {
      this.siteTab = t;
      if (t === 'nginx') { const r = await this.call(() => api('GET', `/api/sites/${this.site.id}/nginx`)); if (r) { this.nginxCustom = r.content; this.vhostPreview = r.vhost; } }
      if (t === 'logs') this.loadLogs();
      if (t === 'sftp') this.loadSftp();
      if (t === 'cache') this.loadRcache();
      if (t === 'stats') this.loadStats();
    },

    // ---------- v2: SFTP ----------
    async loadSftp() {
      const r = await this.call(() => api('GET', `/api/sftp/${this.site.id}`));
      if (r) { this.sftp = r; this.sftpKeys = r.keys; }
    },
    async sftpEnable() {
      const r = await this.call(() => api('POST', `/api/sftp/${this.site.id}/enable`, { password: this.sftpPw, keys: this.sftpKeys }), 'SFTP enabled');
      if (r) { this.sftp = r; this.sftpCreds = { user: r.user, password: r.password }; this.sftpPw = ''; }
    },
    async sftpNewPw() {
      const r = await this.call(() => api('POST', `/api/sftp/${this.site.id}/password`, { password: this.sftpPw }), 'Password changed');
      if (r) { this.sftpCreds = { user: this.sftp.user, password: r.password }; this.sftpPw = ''; }
    },
    async sftpSaveKeys() { await this.call(() => api('PUT', `/api/sftp/${this.site.id}/keys`, { keys: this.sftpKeys }), 'Keys saved'); },
    async sftpDisable() {
      if (!confirm('Disable SFTP access for this site?')) return;
      if (await this.call(() => api('POST', `/api/sftp/${this.site.id}/disable`), 'SFTP disabled')) { this.sftpCreds = null; this.loadSftp(); }
    },

    // ---------- v2: Cloudflare DNS ----------
    async loadZones() {
      const r = await this.call(() => api('GET', '/api/cloudflare/zones'));
      if (r) { this.zones = r; if (!this.zone && r.length) this.openZone(r[0]); }
    },
    async openZone(z) {
      this.zone = z; this.records = []; this.recForm = null; this.zoneSet = {};
      await this.loadRecords();
      const s = await api('GET', `/api/cloudflare/zones/${z.id}/settings`).catch(() => null);
      if (s) this.zoneSet = s;
    },
    async loadRecords() { const r = await this.call(() => api('GET', `/api/cloudflare/zones/${this.zone.id}/records`)); if (r) this.records = r; },
    filteredRecords() {
      const f = this.recFilter.toLowerCase();
      return f ? this.records.filter(r => (r.name + ' ' + r.type + ' ' + r.content).toLowerCase().includes(f)) : this.records;
    },
    newRecord() { this.recForm = { id: null, type: 'A', name: '', content: this.settings.detected_ip || '', ttl: 1, proxied: true, priority: 10, comment: '' }; },
    editRecord(r) { this.recForm = { ...r, priority: r.priority ?? 10, comment: r.comment || '' }; },
    async saveRecord() {
      const f = this.recForm, body = { type: f.type, name: f.name, content: f.content, ttl: +f.ttl, proxied: !!f.proxied, priority: f.type === 'MX' ? +f.priority : null, comment: f.comment };
      const url = `/api/cloudflare/zones/${this.zone.id}/records` + (f.id ? `/${f.id}` : '');
      if (await this.call(() => api(f.id ? 'PUT' : 'POST', url, body), 'Record saved')) { this.recForm = null; this.loadRecords(); }
    },
    async toggleProxy(r) {
      const body = { type: r.type, name: r.name, content: r.content, ttl: r.ttl, proxied: !r.proxied, priority: r.priority, comment: r.comment || '' };
      if (await this.call(() => api('PUT', `/api/cloudflare/zones/${this.zone.id}/records/${r.id}`, body))) this.loadRecords();
    },
    async deleteRecord(r) {
      if (!confirm(`Delete ${r.type} ${r.name} → ${r.content}?`)) return;
      if (await this.call(() => api('DELETE', `/api/cloudflare/zones/${this.zone.id}/records/${r.id}`), 'Record deleted')) this.loadRecords();
    },
    async setZone(key, value) {
      if (await this.call(() => api('PATCH', `/api/cloudflare/zones/${this.zone.id}/settings/${key}`, { value: String(value) }), `${key} → ${value}`)) this.zoneSet[key] = value;
    },
    async purgeAll() {
      if (!confirm(`Purge ALL cached files for ${this.zone.name}?`)) return;
      await this.call(() => api('POST', `/api/cloudflare/zones/${this.zone.id}/purge`, { everything: true }), 'Cache purged');
    },
    async purgeUrlsNow() {
      const files = this.purgeUrls.split('\n').map(s => s.trim()).filter(Boolean);
      if (await this.call(() => api('POST', `/api/cloudflare/zones/${this.zone.id}/purge`, { files }), `${files.length} URL(s) purged`)) this.purgeUrls = '';
    },

    // ---------- v2: Firewall ----------
    async loadFw() { const r = await this.call(() => api('GET', '/api/firewall')); if (r) this.fw = r; },
    async fwAdd() {
      if (await this.call(() => api('POST', '/api/firewall/rules', this.fwForm), 'Rule added')) { this.fwForm.port = ''; this.fwForm.source = ''; this.loadFw(); }
    },
    async fwDelete(r) {
      if (!confirm(`Delete rule #${r.num}: ${r.to} ${r.action} ${r.from}?`)) return;
      if (await this.call(() => api('DELETE', `/api/firewall/rules/${r.num}`), 'Rule deleted')) this.loadFw();
    },
    async fwBlock() {
      if (!this.blockIp || !confirm(`Block all traffic from ${this.blockIp}?`)) return;
      if (await this.call(() => api('POST', '/api/firewall/block', { ip: this.blockIp }), 'IP blocked')) { this.blockIp = ''; this.loadFw(); }
    },
    async fwToggle(on) {
      if (!on && !confirm('Disable the firewall? All ports become reachable.')) return;
      if (await this.call(() => api('POST', '/api/firewall/toggle', { enabled: on }), on ? 'Firewall enabled' : 'Firewall disabled')) this.loadFw();
    },
    async unban(jail, ip) { if (await this.call(() => api('POST', '/api/firewall/unban', { jail, ip }), `${ip} unbanned`)) this.loadFw(); },

    // ---------- v2: 2FA ----------
    async loadTfa() { const r = await api('GET', '/api/security/2fa').catch(() => null); if (r) this.tfa = r; },
    async tfaStart() {
      const r = await this.call(() => api('POST', '/api/security/2fa/setup'));
      if (!r) return;
      let svg = '';
      try { const q = qrcode(0, 'M'); q.addData(r.uri); q.make(); svg = q.createSvgTag({ cellSize: 5, margin: 3, scalable: true }); } catch (e) { }
      this.tfaSetup = { ...r, svg }; this.tfaCode = ''; this.tfaCodes = null;
    },
    async tfaEnable() {
      const r = await this.call(() => api('POST', '/api/security/2fa/enable', { code: this.tfaCode }), '2FA enabled ✔');
      if (r) { this.tfaCodes = r.recovery_codes; this.tfaSetup = null; this.loadTfa(); }
    },
    async tfaDisable() {
      if (await this.call(() => api('POST', '/api/security/2fa/disable', { password: this.tfaPw }), '2FA disabled')) { this.tfaPw = ''; this.loadTfa(); }
    },
    async tfaRegen() {
      const r = await this.call(() => api('POST', '/api/security/2fa/recovery', { password: this.tfaPw }), 'New recovery codes generated');
      if (r) { this.tfaCodes = r.recovery_codes; this.tfaPw = ''; this.loadTfa(); }
    },

    // ---------- v2: remote backups ----------
    async saveRemote() {
      const body = { ...this.remoteForm, keep_days: +this.remoteForm.keep_days };
      if (await this.call(() => api('PUT', '/api/settings/remote', body), 'Remote storage saved')) { this.remoteForm.secret = ''; this.loadSettings(); }
    },
    async testRemote() { const r = await this.call(() => api('POST', '/api/settings/remote/test')); if (r) this.toast(`Connected ✔ (${r.objects} item(s) at the path)`); },
    async loadRemoteFiles() {
      if (!this.bkSite) return this.toast('Choose a site first', 'err');
      const r = await this.call(() => api('GET', `/api/backups/remote?site_id=${this.bkSite}`));
      if (r) this.remoteFiles = r;
    },
    async fetchRemote(f) {
      if (await this.call(() => api('POST', '/api/backups/remote/fetch', { site_id: +this.bkSite, name: f.name }), 'Downloading from remote…')) this.loadBackups();
    },
    async uploadBackup(b) { if (await this.call(() => api('POST', `/api/backups/${b.id}/upload`), 'Upload started')) setTimeout(() => this.loadBackups(), 1500); },
    async issueSsl() {
      this.toast('Requesting certificate… (up to a minute)');
      const r = await this.call(() => api('POST', `/api/sites/${this.site.id}/ssl`, { method: this.sslMethod }), 'SSL certificate installed ✔');
      if (r) this.reloadSite();
    },
    async removeSsl() {
      if (!confirm('Remove SSL and revert to HTTP?')) return;
      if (await this.call(() => api('DELETE', `/api/sites/${this.site.id}/ssl`), 'SSL removed')) this.reloadSite();
    },
    async cfDns(proxied) {
      const r = await this.call(() => api('POST', '/api/cloudflare/dns', { site_id: this.site.id, proxied }));
      if (r) this.toast(`DNS → ${r.ip}: ` + Object.entries(r.records).map(([k, v]) => `${k} ${v}`).join(', '));
    },
    async loadUsage() { const r = await this.call(() => api('GET', `/api/sites/${this.site.id}/usage`)); if (r) this.usage = r.bytes; },
    async changePhp(v) {
      if (await this.call(() => api('PUT', `/api/sites/${this.site.id}/php`, { version: v }), `PHP switched to ${v}`)) this.reloadSite();
    },
    async saveIni() {
      const body = { ...this.ini, max_execution_time: +this.ini.max_execution_time, max_input_time: +this.ini.max_input_time,
        max_input_vars: +this.ini.max_input_vars, max_children: +this.ini.max_children };
      if (await this.call(() => api('PUT', `/api/sites/${this.site.id}/ini`, body), 'PHP settings saved')) this.reloadSite();
    },
    async saveNginx() { await this.call(() => api('PUT', `/api/sites/${this.site.id}/nginx`, { content: this.nginxCustom }), 'nginx reloaded ✔'); },
    async loadLogs() {
      const r = await this.call(() => api('GET', `/api/sites/${this.site.id}/logs?kind=${this.logKind}&lines=300`));
      if (r) this.logText = r.content || '(empty)';
    },
    async deleteSite() {
      const d = this.site.domain;
      if (prompt(`Type the domain to permanently delete it:\n${d}`) !== d) return this.toast('Cancelled', 'err');
      if (await this.call(() => api('DELETE', `/api/sites/${this.site.id}?delete_files=${this.del.files}&delete_dbs=${this.del.dbs}`), `${d} deleted`)) {
        this.site = null; this.go('sites');
      }
    },

    // ---------- databases ----------
    async loadDbs() { await this.call(async () => { this.dbs = await api('GET', '/api/databases'); }); },
    async createDb() {
      const r = await this.call(() => api('POST', '/api/databases', { site_id: +this.dbForm.site_id, name: this.dbForm.name }));
      if (r) { this.dbCreds = r; this.dbForm.name = ''; this.loadDbs(); }
    },
    async resetDbPw(d) {
      if (!confirm(`Generate a new password for ${d.user}? Apps using the old one will break until updated.`)) return;
      const r = await this.call(() => api('PUT', `/api/databases/${d.id}/password`, {}));
      if (r) this.dbCreds = { name: d.name, user: r.user, password: r.password, host: 'localhost' };
    },
    async deleteDb(d) {
      if (prompt(`Type the database name to DROP it:\n${d.name}`) !== d.name) return;
      if (await this.call(() => api('DELETE', `/api/databases/${d.id}`), 'Database dropped')) this.loadDbs();
    },

    // ---------- file manager ----------
    async fmList(path) {
      if (path !== undefined) this.fm.path = path;
      const r = await this.call(() => api('GET', `/api/files/${this.fm.site_id}/list?path=${encodeURIComponent(this.fm.path)}`));
      if (r) { this.fm.items = r.items; this.fm.path = r.path; this.fm.truncated = r.truncated; this.fm.sel = []; }
    },
    fmJoin(name) { return this.fm.path ? this.fm.path + '/' + name : name; },
    fmCrumbs() {
      const parts = this.fm.path ? this.fm.path.split('/') : [];
      return parts.map((p, i) => ({ name: p, path: parts.slice(0, i + 1).join('/') }));
    },
    fmUp() { const p = this.fm.path.split('/'); p.pop(); this.fmList(p.join('/')); },
    fmOpen(it) { if (it.dir) this.fmList(this.fmJoin(it.name)); else this.fmEdit(it); },
    fmToggleAll(e) { this.fm.sel = e.target.checked ? this.fm.items.map(i => i.name) : []; },
    async fmEdit(it) {
      const path = this.fmJoin(it.name);
      const r = await this.call(() => api('GET', `/api/files/${this.fm.site_id}/read?path=${encodeURIComponent(path)}`));
      if (r) this.editor = { path, content: r.content, dirty: false };
    },
    async fmSave() {
      if (await this.call(() => api('POST', `/api/files/${this.fm.site_id}/write`, { path: this.editor.path, content: this.editor.content }), 'Saved')) {
        this.editor.dirty = false; this.fmList();
      }
    },
    closeEditor() { if (this.editor.dirty && !confirm('Discard unsaved changes?')) return; this.editor = null; },
    editorKey(e) {
      if (e.key === 'Tab') { e.preventDefault(); const t = e.target, s = t.selectionStart; t.setRangeText('    ', s, t.selectionEnd, 'end'); this.editor.content = t.value; this.editor.dirty = true; }
      if ((e.ctrlKey || e.metaKey) && e.key === 's') { e.preventDefault(); this.fmSave(); }
    },
    async fmMkdir() { const n = prompt('New folder name'); if (n && await this.call(() => api('POST', `/api/files/${this.fm.site_id}/mkdir`, { path: this.fm.path, name: n }))) this.fmList(); },
    async fmNewFile() { const n = prompt('New file name'); if (n && await this.call(() => api('POST', `/api/files/${this.fm.site_id}/newfile`, { path: this.fm.path, name: n }))) { await this.fmList(); this.fmEdit({ name: n }); } },
    async fmRename(it) {
      const n = prompt('Rename to', it.name);
      if (n && n !== it.name && await this.call(() => api('POST', `/api/files/${this.fm.site_id}/rename`, { path: this.fmJoin(it.name), new_name: n }))) this.fmList();
    },
    async fmChmod(it) {
      const m = prompt(`Permissions for ${it.name} (octal)`, it.mode);
      if (m && await this.call(() => api('POST', `/api/files/${this.fm.site_id}/chmod`, { path: this.fmJoin(it.name), mode: m }), 'Permissions updated')) this.fmList();
    },
    async fmDelete(names) {
      names = names || this.fm.sel;
      if (!names.length || !confirm(`Delete ${names.length} item(s)?\n${names.slice(0, 8).join('\n')}${names.length > 8 ? '\n…' : ''}`)) return;
      if (await this.call(() => api('POST', `/api/files/${this.fm.site_id}/delete`, { path: this.fm.path, items: names }), 'Deleted')) this.fmList();
    },
    async fmExtract(it) {
      this.toast('Extracting…');
      if (await this.call(() => api('POST', `/api/files/${this.fm.site_id}/extract`, { path: this.fmJoin(it.name) }), 'Extracted ✔')) this.fmList();
    },
    async fmCompress() {
      if (!this.fm.sel.length) return;
      const n = prompt('Archive name', 'archive.zip');
      if (n && await this.call(() => api('POST', `/api/files/${this.fm.site_id}/compress`, { path: this.fm.path, items: this.fm.sel, name: n }), 'Archive created')) this.fmList();
    },
    async fmUpload(e) {
      const files = e.target.files; if (!files.length) return;
      const fd = new FormData(); fd.append('path', this.fm.path);
      for (const f of files) fd.append('files', f);
      this.toast(`Uploading ${files.length} file(s)…`);
      if (await this.call(() => api('POST', `/api/files/${this.fm.site_id}/upload`, fd, true), 'Upload complete ✔')) this.fmList();
      e.target.value = '';
    },
    fmDownloadUrl(it) { return `/api/files/${this.fm.site_id}/download?path=${encodeURIComponent(this.fmJoin(it.name))}`; },

    // ---------- cron ----------
    async loadCrons() { await this.call(async () => { this.crons = await api('GET', '/api/cron' + (this.cronSite ? `?site_id=${this.cronSite}` : '')); }); },
    async addCron() {
      if (await this.call(() => api('POST', '/api/cron', { ...this.cronForm, site_id: +this.cronForm.site_id }), 'Cron job added')) { this.cronForm.command = ''; this.loadCrons(); }
    },
    async toggleCron(c) {
      if (await this.call(() => api('PUT', `/api/cron/${c.id}`, { schedule: c.schedule, command: c.command, enabled: !c.enabled }))) this.loadCrons();
    },
    async editCron(c) {
      const s = prompt('Schedule', c.schedule); if (!s) return;
      const cmd = prompt('Command', c.command); if (!cmd) return;
      if (await this.call(() => api('PUT', `/api/cron/${c.id}`, { schedule: s, command: cmd, enabled: !!c.enabled }), 'Updated')) this.loadCrons();
    },
    async deleteCron(c) { if (confirm('Delete this cron job?') && await this.call(() => api('DELETE', `/api/cron/${c.id}`), 'Deleted')) this.loadCrons(); },

    // ---------- backups ----------
    async loadBackups(silent) {
      const f = async () => { this.backups = await api('GET', '/api/backups' + (this.bkSite ? `?site_id=${this.bkSite}` : '')); };
      silent ? await f().catch(() => { }) : await this.call(f);
      const active = this.backups.some(b => b.status === 'running' || b.status === 'restoring');
      if (active && !this.bkTimer) this.bkTimer = setInterval(() => this.loadBackups(true), 4000);
      if (!active && this.bkTimer) { clearInterval(this.bkTimer); this.bkTimer = null; }
    },
    async backupNow() {
      if (!this.bkSite) return this.toast('Choose a site first', 'err');
      if (await this.call(() => api('POST', '/api/backups', { site_id: +this.bkSite }), 'Backup started')) this.loadBackups();
    },
    async restoreBackup(b) {
      if (prompt(`Restore ${b.domain} from ${fmtDate(b.created)}?\nCurrent files & databases will be OVERWRITTEN.\nType RESTORE to confirm.`) !== 'RESTORE') return;
      if (await this.call(() => api('POST', `/api/backups/${b.id}/restore`), 'Restore started')) this.loadBackups();
    },
    async deleteBackup(b) { if (confirm('Delete this backup file?') && await this.call(() => api('DELETE', `/api/backups/${b.id}`), 'Deleted')) this.loadBackups(); },

    // ---------- installers ----------
    genPw(n = 18) { const c = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789'; const a = crypto.getRandomValues(new Uint32Array(n)); return Array.from(a, x => c[x % c.length]).join(''); },
    async installWp() {
      this.toast('Installing WordPress… (≈30s)');
      const r = await this.call(() => api('POST', '/api/install/wordpress', { ...this.wp, site_id: +this.wp.site_id }));
      if (r) this.installResult = { type: 'WordPress', ...r, user: this.wp.admin_user, pass: this.wp.admin_password };
    },
    async gitDeploy() {
      this.toast('Cloning repository…');
      if (await this.call(() => api('POST', '/api/install/git', { ...this.git, site_id: +this.git.site_id }), 'Deployed from Git ✔')) this.git.repo = '';
    },
    async cloneSite() {
      if (!confirm(`Copy files from ${this.siteName(this.clone.source_id)} to ${this.siteName(this.clone.target_id)}?`)) return;
      this.toast('Copying files…');
      await this.call(() => api('POST', '/api/install/clone', { ...this.clone, source_id: +this.clone.source_id, target_id: +this.clone.target_id }), 'Files cloned ✔');
    },

    // ---------- settings ----------
    async loadSettings() {
      await this.call(async () => {
        this.settings = await api('GET', '/api/settings');
        const r = this.settings.remote || {};
        this.remoteForm = { enabled: r.enabled, provider: r.provider || 'Cloudflare', endpoint: r.endpoint || '', region: r.region || 'auto',
          bucket: r.bucket || '', access_key: r.access_key || '', secret: '', prefix: r.prefix || 'ypanel', keep_days: r.keep_days || 30 };
      });
    },
    async saveSettings() {
      const s = this.settings;
      const body = { admin_email: s.admin_email, server_ip: s.server_ip, backup_keep: +s.backup_keep, backup_exclude: s.backup_exclude,
        backup_hour: String(s.backup_hour), default_php: s.default_php };
      if (await this.call(() => api('PUT', '/api/settings', body), 'Settings saved')) this.loadSettings();
    },
    async verifyCf() {
      const r = await this.call(() => api('POST', '/api/cloudflare/verify', { token: this.cfToken }));
      if (r) { this.cfInfo = r; this.toast(`Token ${r.status} — ${r.zones.length} zone(s)`); }
    },
    async saveCf(clear) {
      if (await this.call(() => api('PUT', '/api/settings', { cf_token: clear ? '' : this.cfToken }), clear ? 'Token removed' : 'Cloudflare token saved')) { this.cfToken = ''; this.loadSettings(); }
    },
// ---------- v3: users ----------
    async loadUsers() { const r = await this.call(() => api('GET', '/api/users')); if (r) this.users = r; },
    newUser() { this.userForm = { id: null, username: '', password: '', role: 'user', sites: [] }; this.userCreds = null; },
    editUser(u) { this.userForm = { id: u.id, username: u.username, password: '', role: u.role, sites: u.sites.map(String) }; this.userCreds = null; },
    async saveUser() {
      const f = this.userForm, sites = f.sites.map(Number);
      const r = f.id
        ? await this.call(() => api('PUT', `/api/users/${f.id}`, { role: f.role, sites, ...(f.password ? { password: f.password } : {}) }), 'User updated')
        : await this.call(() => api('POST', '/api/users', { username: f.username, password: f.password, role: f.role, sites }), 'User created');
      if (r) { if (r.password) this.userCreds = { username: f.username, password: r.password }; this.userForm = null; this.loadUsers(); }
    },
    async resetUserPw(u) {
      if (!confirm(`Generate a new password for ${u.username}?`)) return;
      const r = await this.call(() => api('PUT', `/api/users/${u.id}`, { password: '' }));
      if (r) this.userCreds = { username: u.username, password: r.password };
    },
    async resetUser2fa(u) { if (confirm(`Turn off 2FA for ${u.username}?`) && await this.call(() => api('POST', `/api/users/${u.id}/2fa-off`), '2FA disabled')) this.loadUsers(); },
    async deleteUser(u) { if (confirm(`Delete user ${u.username}?`) && await this.call(() => api('DELETE', `/api/users/${u.id}`), 'User deleted')) this.loadUsers(); },

    // ---------- v3: redis ----------
    async loadRcache() { const r = await this.call(() => api('GET', `/api/redis/${this.site.id}`)); if (r) this.rcache = r; },
    async rcacheEnable() { const r = await this.call(() => api('POST', `/api/redis/${this.site.id}/enable`), 'Redis enabled'); if (r) this.rcache = r; },
    async rcacheWp() {
      this.toast('Installing Redis Object Cache plugin…');
      const r = await this.call(() => api('POST', `/api/redis/${this.site.id}/wordpress`), 'WordPress connected to Redis ✔'); if (r) this.rcache = r;
    },
    async rcacheFlush() { const r = await this.call(() => api('POST', `/api/redis/${this.site.id}/flush`)); if (r) this.toast(`${r.deleted} keys flushed`); },
    async rcacheDisable() { if (confirm('Disable Redis for this site?') && await this.call(() => api('POST', `/api/redis/${this.site.id}/disable`), 'Redis disabled')) this.loadRcache(); },
    async loadRedisServer() { this.rserver = await api('GET', '/api/redis/server').catch(() => null); },
    async setRedisMem() {
      const v = prompt('Redis max memory (e.g. 128mb, 256mb, 1gb)', this.settings.redis_maxmemory || '128mb');
      if (v && await this.call(() => api('PUT', '/api/redis/server', { maxmemory: v }), 'Redis memory updated')) this.loadRedisServer();
    },

    // ---------- v3: stats ----------
    async loadStats(refresh) {
      this.tstats = null;
      const r = await this.call(() => api('GET', `/api/stats/${this.site.id}?days=${this.statsDays}${refresh ? '&refresh=true' : ''}`));
      if (r) this.tstats = r;
    },
    statMax(key) { return this.tstats ? Math.max(1, ...this.tstats.daily.map(d => d[key])) : 1; },

    // ---------- v3: mail ----------
    async loadMail() {
      const r = await this.call(() => api('GET', '/api/mail/status'));
      if (!r) return;
      this.mail = r; if (!this.mailHost) this.mailHost = r.hostname || '';
      if (r.setup === 'running' && !this.mailTimer) this.mailTimer = setInterval(() => this.pollMail(), 4000);
      if (r.installed) this.mailTabOpen(this.mailTab);
    },
    async pollMail() {
      const r = await api('GET', '/api/mail/status').catch(() => null); if (!r) return;
      this.mail = r;
      if (r.setup !== 'running') { clearInterval(this.mailTimer); this.mailTimer = null; this.toast(r.setup === 'done' ? 'Mail server installed ✔' : 'Mail setup failed — see log', r.setup === 'done' ? 'ok' : 'err'); this.loadMail(); }
    },
    async installMail() {
      if (!confirm(`Install Postfix + Dovecot + Rspamd + Roundcube with hostname ${this.mailHost}? (takes 2-5 minutes)`)) return;
      if (await this.call(() => api('POST', '/api/mail/install', { hostname: this.mailHost }), 'Installation started')) this.loadMail();
    },
    async runMailChecks() { this.mailChecks = null; const r = await this.call(() => api('GET', '/api/mail/checks')); if (r) this.mailChecks = r; },
    async mailTls() { this.toast('Requesting certificate…'); if (await this.call(() => api('POST', '/api/mail/tls'), 'Mail TLS certificate installed ✔')) this.loadMail(); },
    async mailTabOpen(t) {
      this.mailTab = t;
      if (t === 'domains') { const r = await this.call(() => api('GET', '/api/mail/domains')); if (r) this.mdomains = r; }
      if (t === 'mailboxes') { const [d, b] = await Promise.all([api('GET', '/api/mail/domains'), this.call(() => api('GET', '/api/mail/mailboxes?usage=true'))]); this.mdomains = d; if (b) this.mboxes = b; }
      if (t === 'aliases') { const [d, a] = await Promise.all([api('GET', '/api/mail/domains'), this.call(() => api('GET', '/api/mail/aliases'))]); this.mdomains = d; if (a) this.maliases = a; }
      if (t === 'queue') { const q = await this.call(() => api('GET', '/api/mail/queue')); if (q) this.mqueue = q; }
    },
    async addMailDomain() {
      const r = await this.call(() => api('POST', '/api/mail/domains', { domain: this.mdomForm.domain, site_id: this.mdomForm.site_id ? +this.mdomForm.site_id : null }), 'Domain added (DKIM key generated)');
      if (r) { this.mdomForm = { domain: '', site_id: '' }; await this.mailTabOpen('domains'); this.showMailDns(this.mdomains.find(d => d.id === r.id)); }
    },
    async showMailDns(d, check) {
      this.mdnsDomain = d; this.mdns = null;
      const r = await this.call(() => api('GET', `/api/mail/domains/${d.id}/dns${check ? '?check=true' : ''}`)); if (r) this.mdns = r;
    },
    async mailDnsCloudflare() {
      const r = await this.call(() => api('POST', `/api/mail/domains/${this.mdnsDomain.id}/dns/cloudflare`));
      if (r) { this.toast(r.map(x => `${x.record}: ${x.result}`).join('\n')); setTimeout(() => this.showMailDns(this.mdnsDomain, true), 4000); }
    },
    async deleteMailDomain(d) {
      if (prompt(`Type the domain to remove it with all its mailboxes and forwarders:\n${d.domain}`) !== d.domain) return;
      const wipe = confirm('Also delete the stored emails on disk?');
      if (await this.call(() => api('DELETE', `/api/mail/domains/${d.id}?delete_mail=${wipe}`), 'Domain removed')) { this.mdns = null; this.mailTabOpen('domains'); }
    },
    async addMailbox() {
      const f = this.mboxForm;
      const r = await this.call(() => api('POST', '/api/mail/mailboxes', { domain_id: +f.domain_id, local: f.local, password: f.password, quota_mb: +f.quota_mb }), 'Mailbox created');
      if (r) { this.mailCreds = { address: r.address, password: r.password }; this.mboxForm.local = ''; this.mboxForm.password = ''; this.mailTabOpen('mailboxes'); }
    },
    async mailboxPw(m) {
      const pw = prompt(`New password for ${m.address} (empty = generate)`, ''); if (pw === null) return;
      const r = await this.call(() => api('PUT', `/api/mail/mailboxes/${m.id}`, { password: pw }));
      if (r) this.mailCreds = { address: m.address, password: r.password };
    },
    async mailboxQuota(m) {
      const q = prompt(`Quota for ${m.address} in MB (0 = unlimited)`, m.quota_mb); if (q === null) return;
      if (await this.call(() => api('PUT', `/api/mail/mailboxes/${m.id}`, { quota_mb: +q }), 'Quota updated')) this.mailTabOpen('mailboxes');
    },
    async mailboxToggle(m) { if (await this.call(() => api('PUT', `/api/mail/mailboxes/${m.id}`, { enabled: !m.enabled }), m.enabled ? 'Suspended' : 'Activated')) this.mailTabOpen('mailboxes'); },
    async deleteMailbox(m) {
      if (prompt(`Type the address to delete it and its emails:\n${m.address}`) !== m.address) return;
      if (await this.call(() => api('DELETE', `/api/mail/mailboxes/${m.id}`), 'Mailbox deleted')) this.mailTabOpen('mailboxes');
    },
    async addAlias() {
      const f = this.maliasForm;
      if (await this.call(() => api('POST', '/api/mail/aliases', { domain_id: +f.domain_id, local: f.local, destinations: f.destinations }), 'Forwarder created')) { f.local = ''; f.destinations = ''; this.mailTabOpen('aliases'); }
    },
    async editAlias(a) {
      const v = prompt(`Forward ${a.source} to (comma separated)`, a.destinations.split(',').join(', ')); if (!v) return;
      if (await this.call(() => api('PUT', `/api/mail/aliases/${a.id}`, { destinations: v }), 'Forwarder updated')) this.mailTabOpen('aliases');
    },
    async deleteAlias(a) { if (confirm(`Delete forwarder ${a.source}?`) && await this.call(() => api('DELETE', `/api/mail/aliases/${a.id}`), 'Forwarder deleted')) this.mailTabOpen('aliases'); },
    async flushQueue() { if (await this.call(() => api('POST', '/api/mail/queue/flush'), 'Queue flushed — retrying delivery')) setTimeout(() => this.mailTabOpen('queue'), 2000); },
    async deleteQueued(q) { if (confirm(q === 'ALL' ? 'Delete ALL queued messages?' : `Delete message ${q}?`) && await this.call(() => api('DELETE', `/api/mail/queue/${q}`), 'Deleted')) this.mailTabOpen('queue'); },

    async changePw() {
      if (await this.call(() => api('POST', '/api/security/password', this.pw), 'Password changed')) this.pw = { current: '', new: '' };
    },
  };
}
