import { useId, useState } from 'react';
import { AlertTriangle, Bot, Plus } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import api from '../../services/api';
import { useAuth } from '../../contexts/useAuth.js';
import useSettingFocus from '../../hooks/useSettingFocus';
import CopyField from '../CopyField';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';

const KEY_PLACEHOLDER = '<your-mcp-key>';

// Settings → API → MCP: the endpoint AI assistants call, a key scoped to it,
// and ready-to-paste client configuration. The raw key is shown once, right
// after it is created, exactly like the API keys table's own create flow.
const McpSettingsCard = () => {
    const { t } = useTranslation();
    const fieldId = useId();
    const register = useSettingFocus();
    const { isDeveloper } = useAuth();
    const [name, setName] = useState('claude');
    const [rawKey, setRawKey] = useState(null);
    const [creating, setCreating] = useState(false);
    const [error, setError] = useState(null);

    const endpoint = api.getMcpEndpoint();
    const key = rawKey || KEY_PLACEHOLDER;
    const serverName = (name.trim() || 'serverkit').toLowerCase().replace(/[^a-z0-9_-]/g, '-');

    const claudeCode = `claude mcp add --transport http ${serverName} ${endpoint} --header "Authorization: Bearer ${key}"`;
    const genericJson = JSON.stringify({
        mcpServers: {
            [serverName]: {
                type: 'http',
                url: endpoint,
                headers: { Authorization: `Bearer ${key}` },
            },
        },
    }, null, 2);
    const pathUrl = `${endpoint}/k/${key}`;

    const handleCreate = async () => {
        setCreating(true);
        setError(null);
        try {
            const result = await api.createMcpKey(`MCP: ${name.trim() || 'assistant'}`);
            setRawKey(result.raw_key);
        } catch (err) {
            setError(err?.message || t('app.mcpSettingsCard.createFailed', 'Could not create the key'));
        } finally {
            setCreating(false);
        }
    };

    return (
        <div {...register('api-mcp', 'settings-card')}>
            <div className="settings-card__header">
                <div className="settings-card__header-left">
                    <Bot size={20} />
                    <div>
                        <h3>{t('app.mcpSettingsCard.title', 'MCP server')}</h3>
                        <p>{t('app.mcpSettingsCard.subtitle', 'Let an AI assistant publish and run your services. Tools act only on your own services; viewers get read-only tools.')}</p>
                    </div>
                </div>
            </div>

            <div className="mcp-settings">
                <CopyField value={endpoint} label={t('app.mcpSettingsCard.endpoint', 'Endpoint')} />

                {isDeveloper && !rawKey && (
                    <div className="mcp-settings__create">
                        <div className="form-group">
                            <Label htmlFor={`${fieldId}-name`}>{t('app.mcpSettingsCard.serverName', 'Name in your client')}</Label>
                            <Input
                                id={`${fieldId}-name`}
                                value={name}
                                maxLength={40}
                                onChange={(e) => setName(e.target.value)}
                            />
                        </div>
                        <Button variant="default" onClick={handleCreate} disabled={creating}>
                            <Plus size={14} />
                            {creating
                                ? t('app.mcpSettingsCard.creating', 'Creating…')
                                : t('app.mcpSettingsCard.createKey', 'Create MCP key')}
                        </Button>
                    </div>
                )}

                {error && <div className="alert alert--error">{error}</div>}

                {rawKey && (
                    <>
                        <div className="mcp-settings__warning">
                            <AlertTriangle size={16} />
                            <span>{t('app.mcpSettingsCard.copyNow', 'Copy this key now. It will not be shown again; revoke it in the table above if it leaks.')}</span>
                        </div>
                        <CopyField value={rawKey} label={t('app.mcpSettingsCard.key', 'MCP key')} secret />
                    </>
                )}

                <CopyField
                    value={claudeCode}
                    label={t('app.mcpSettingsCard.claudeCode', 'Claude Code')}
                    secret={!!rawKey}
                    multiline
                />
                <CopyField
                    value={genericJson}
                    label={t('app.mcpSettingsCard.genericJson', 'Other MCP clients (JSON)')}
                    secret={!!rawKey}
                    multiline
                />
                <CopyField
                    value={pathUrl}
                    label={t('app.mcpSettingsCard.connectorUrl', 'Connector URL (claude.ai custom connectors)')}
                    secret={!!rawKey}
                />
                <div className="mcp-settings__warning">
                    <AlertTriangle size={16} />
                    <span>{t('app.mcpSettingsCard.urlIsSecret', 'This URL contains your key: anyone who has it can act as you. Use it only where headers cannot be set, never share or commit it.')}</span>
                </div>
            </div>
        </div>
    );
};

export default McpSettingsCard;
