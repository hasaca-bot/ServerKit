// Remote Publish & MCP, contributed through the extension system.
//
// Two slotted widgets, no core page changes:
//  - RemotePublishSettings → Settings, API tab (settings.section slot): the
//    publish gateway / MCP server for AIs that are not on this server (status,
//    address, key reveal/rotate, on/off) and one auto-deploy switch per service.
//  - ServiceAutoDeploy → every service page (service.detail.tab slot): the
//    auto-deploy switch of that service. Core only shows it read-only once a
//    repository is connected.
//
// After sync this file lives at frontend/src/plugins/serverkit-remote-publish/.
import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import './styles/remote-publish.scss';
import api from '../../services/api';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Switch } from '@/components/ui/switch';
import CopyField from '@/components/CopyField';
import { useToast } from '../../contexts/useToast.js';
import { useConfirm } from '../../hooks/useConfirm';
import { errorReason } from '@/utils/errorMessage';

const BASE = '/remote-publish';

// The panel ships no translations for an extension; pick Turkish or English
// from the active UI language.
function useText() {
    const { i18n } = useTranslation();
    const tr = (i18n.language || '').toLowerCase().startsWith('tr');
    return (trText, enText) => (tr ? trText : enText);
}

function AutoDeploySwitch({ service, onChanged, compact }) {
    const L = useText();
    const toast = useToast();
    const [busy, setBusy] = useState(false);

    async function toggle(next) {
        setBusy(true);
        try {
            const row = await api.request(`${BASE}/auto-deploy/${service.id}`, { method: 'PUT', body: { enabled: next } });
            onChanged(row);
            toast.success(next
                ? L(`${service.name}: otomatik yayın açıldı`, `${service.name}: auto deploy on`)
                : L(`${service.name}: otomatik yayın kapatıldı`, `${service.name}: auto deploy off`));
        } catch (e) {
            toast.error(errorReason(e));
        } finally {
            setBusy(false);
        }
    }

    if (!service.git_connected) {
        return <span className="remote-publish__muted">{L('Git bağlı değil', 'No Git deployment')}</span>;
    }
    return (
        <label className={`remote-publish__switch${compact ? ' remote-publish__switch--compact' : ''}`}>
            <Switch checked={service.auto_deploy} disabled={busy} onCheckedChange={toggle}
                aria-label={L('Otomatik yayın', 'Auto deploy')} />
            <span>{service.auto_deploy ? L('Açık', 'On') : L('Kapalı', 'Off')}</span>
        </label>
    );
}

export function RemotePublishSettings({ context }) {
    const L = useText();
    const toast = useToast();
    const { confirm } = useConfirm();
    const [status, setStatus] = useState(null);
    const [services, setServices] = useState([]);
    const [token, setToken] = useState(null);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState(null);
    const visible = context?.tab === 'api';

    const load = useCallback(async () => {
        try {
            const [s, list] = await Promise.all([
                api.request(`${BASE}/status`),
                api.request(`${BASE}/auto-deploy`),
            ]);
            setStatus(s);
            setServices(list.services || []);
            setError(null);
        } catch (e) {
            setError(errorReason(e));
        }
    }, []);

    useEffect(() => { if (visible) load(); }, [visible, load]);

    if (!visible) return null;

    async function reveal() {
        setBusy(true);
        try {
            const r = await api.request(`${BASE}/token/reveal`, { method: 'POST' });
            setToken(r.token);
        } catch (e) {
            toast.error(errorReason(e));
        } finally {
            setBusy(false);
        }
    }

    async function rotate() {
        const ok = await confirm({
            title: L('Anahtarı değiştir', 'Replace key'),
            message: L('Yeni bir bağlantı anahtarı üretilecek; eski anahtarı kullanan her yapay zekâ bağlantısı hemen çalışmaz olur.',
                'A new key is generated; every AI still using the old key stops working immediately.'),
            confirmText: L('Değiştir', 'Replace'),
        });
        if (!ok) return;
        setBusy(true);
        try {
            const r = await api.request(`${BASE}/token/rotate`, { method: 'POST' });
            setToken(r.token);
            await load();
            toast.success(L('Yeni anahtar üretildi', 'New key generated'));
        } catch (e) {
            toast.error(errorReason(e));
        } finally {
            setBusy(false);
        }
    }

    async function setRunning(start) {
        setBusy(true);
        try {
            setStatus(await api.request(`${BASE}/service/${start ? 'start' : 'stop'}`, { method: 'POST' }));
        } catch (e) {
            toast.error(errorReason(e));
        } finally {
            setBusy(false);
        }
    }

    function updateRow(row) {
        setServices((prev) => prev.map((s) => (s.id === row.id ? row : s)));
    }

    const keyText = token || 'YOUR_KEY';
    const claudeCmd = `claude mcp add --transport http serverkit ${status?.mcp_url || ''} --header "Authorization: Bearer ${keyText}"`;
    const mcpJson = JSON.stringify({
        mcpServers: { serverkit: { type: 'http', url: status?.mcp_url || '', headers: { Authorization: `Bearer ${keyText}` } } },
    }, null, 2);

    return (
        <div className="remote-publish">
            <Card>
                <CardHeader>
                    <CardTitle>{L('Uzaktan yayın ve MCP', 'Remote publish & MCP')}</CardTitle>
                    <CardDescription>
                        {L('Bu sunucuda olmayan yapay zekâlar (bulut oturumları, başka bilgisayarlar) bu bağlantı anahtarıyla GitHub depolarını https://<ad>.tadadigital.shop olarak yayınlayabilir. Mevcut sitelere ve panele dokunamazlar; silme yetkileri yoktur.',
                            'AI assistants that are not on this server can publish GitHub repositories as https://<name>.tadadigital.shop with this key. They cannot touch existing sites or the panel, and cannot delete anything.')}
                    </CardDescription>
                </CardHeader>
                <CardContent>
                    {error && <p className="remote-publish__error">{error}</p>}
                    {status && (
                        <>
                            <div className="remote-publish__row">
                                <span>{L('Durum', 'Status')}</span>
                                {status.installed
                                    ? <Badge variant={status.active ? 'success' : 'secondary'}>
                                        {status.active ? L('Çalışıyor', 'Running') : L('Kapalı', 'Stopped')}
                                    </Badge>
                                    : <Badge variant="secondary">{L('Kurulu değil', 'Not installed')}</Badge>}
                                {status.installed && (
                                    <Button size="sm" variant="outline" disabled={busy} onClick={() => setRunning(!status.active)}>
                                        {status.active ? L('Kapat', 'Stop') : L('Aç', 'Start')}
                                    </Button>
                                )}
                            </div>
                            <div className="remote-publish__field">
                                <span>{L('MCP adresi', 'MCP endpoint')}</span>
                                <CopyField value={status.mcp_url} />
                            </div>
                            <div className="remote-publish__field">
                                <span>{L('REST adresi (talimatlar)', 'REST endpoint (instructions)')}</span>
                                <CopyField value={status.url} />
                            </div>
                            <div className="remote-publish__field">
                                <span>{L('Bağlantı anahtarı', 'Access key')}</span>
                                {token
                                    ? <CopyField value={token} />
                                    : <span className="remote-publish__muted">{status.token_hint || L('yok', 'none')}</span>}
                                <div className="remote-publish__actions">
                                    {!token && status.token_set && (
                                        <Button size="sm" variant="outline" disabled={busy} onClick={reveal}>{L('Göster', 'Reveal')}</Button>
                                    )}
                                    <Button size="sm" variant="outline" disabled={busy || !status.installed} onClick={rotate}>
                                        {L('Yeni anahtar üret', 'Generate new key')}
                                    </Button>
                                </div>
                            </div>
                            <div className="remote-publish__field">
                                <span>{L('Claude Code’a ekle (komut)', 'Add to Claude Code (command)')}</span>
                                <CopyField value={claudeCmd} />
                            </div>
                            <div className="remote-publish__field">
                                <span>{L('Diğer MCP istemcileri (JSON)', 'Other MCP clients (JSON)')}</span>
                                <pre className="remote-publish__code">{mcpJson}</pre>
                            </div>
                            {!token && (
                                <p className="remote-publish__muted">
                                    {L('Komut ve JSON, anahtar gösterilince gerçek anahtarla dolar.', 'The command and JSON fill in the real key once it is revealed.')}
                                </p>
                            )}
                        </>
                    )}
                </CardContent>
            </Card>

            <Card>
                <CardHeader>
                    <CardTitle>{L('Otomatik yayın', 'Auto deploy')}</CardTitle>
                    <CardDescription>
                        {L('Açık olan servisler, GitHub dalına yeni commit gönderildiğinde birkaç dakika içinde kendiliğinden yeniden yayınlanır.',
                            'Services switched on are republished within a few minutes of a new commit on their GitHub branch.')}
                    </CardDescription>
                </CardHeader>
                <CardContent>
                    <table className="remote-publish__table">
                        <thead>
                            <tr>
                                <th>{L('Servis', 'Service')}</th>
                                <th>{L('Adres', 'Address')}</th>
                                <th>{L('Otomatik yayın', 'Auto deploy')}</th>
                            </tr>
                        </thead>
                        <tbody>
                            {services.map((s) => (
                                <tr key={s.id}>
                                    <td>{s.name}</td>
                                    <td>{s.url ? <a href={s.url} target="_blank" rel="noopener noreferrer">{s.url.replace(/^https?:\/\//, '')}</a> : '—'}</td>
                                    <td><AutoDeploySwitch service={s} onChanged={updateRow} compact /></td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </CardContent>
            </Card>
        </div>
    );
}

export function ServiceAutoDeploy({ context }) {
    const L = useText();
    const [service, setService] = useState(null);
    const serviceId = context?.serviceId;

    useEffect(() => {
        if (!serviceId) return;
        let alive = true;
        api.request(`${BASE}/auto-deploy/${serviceId}`)
            .then((row) => { if (alive) setService(row); })
            .catch(() => { if (alive) setService(null); });
        return () => { alive = false; };
    }, [serviceId]);

    if (!service || !service.git_connected) return null;
    return (
        <div className="remote-publish remote-publish--service">
            <Card>
                <CardContent className="remote-publish__service">
                    <div>
                        <strong>{L('Otomatik yayın', 'Auto deploy')}</strong>
                        <p className="remote-publish__muted">
                            {L('GitHub’a yeni commit gelince bu servis kendiliğinden yeniden yayınlanır.',
                                'Republish this service by itself when its GitHub branch gets a new commit.')}
                        </p>
                    </div>
                    <AutoDeploySwitch service={service} onChanged={setService} />
                </CardContent>
            </Card>
        </div>
    );
}
