import { useState } from 'react';
import { Filter } from 'lucide-react';
import api from '../../services/api';
import { useToast } from '../../contexts/useToast.js';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { useTranslation } from 'react-i18next';
import { toastError } from '@/utils/errorMessage';

const toLines = (list) => (Array.isArray(list) ? list.join('\n') : '');
const fromLines = (text) => text.split('\n').map((line) => line.trim()).filter(Boolean);

// Monorepo auto-deploy controls. A push deploys only when at least one changed
// file is under the root directory, matches an include glob (if any are set)
// and matches no ignore glob. Manual deploys always run.
// Must match app/services/deploy_control_service.py.
const BuildFilterPanel = ({ app, onChanged }) => {
    const { t } = useTranslation();
    const toast = useToast();
    const filter = app.build_filter || {};
    const [rootDir, setRootDir] = useState(app.root_dir || '');
    const [paths, setPaths] = useState(toLines(filter.paths));
    const [ignored, setIgnored] = useState(toLines(filter.ignored_paths));
    const [saving, setSaving] = useState(false);

    const dirty = rootDir.trim() !== (app.root_dir || '')
        || paths !== toLines(filter.paths)
        || ignored !== toLines(filter.ignored_paths);

    async function handleSave() {
        setSaving(true);
        try {
            await api.updateApp(app.id, {
                root_dir: rootDir.trim(),
                build_filter: { paths: fromLines(paths), ignored_paths: fromLines(ignored) },
            });
            toast.success(t('app.buildFilterPanel.saved', 'Auto-deploy filters saved'));
            onChanged?.();
        } catch (err) {
            toastError(toast, t('app.buildFilterPanel.saveFailed', "Couldn't save the auto-deploy filters."), err);
        } finally {
            setSaving(false);
        }
    }

    return (
        <div className="app-panel">
            <div className="app-panel-header">
                <Filter />
                <span>{t('app.buildFilterPanel.title', 'Auto-deploy filters')}</span>
            </div>
            <div className="app-panel-body">
                <p className="app-panel-hint">
                    {t('app.buildFilterPanel.intro', 'A push deploys only if at least one changed file matches. Leave everything empty to deploy on every push. Manual deploys always run.')}
                </p>
                <p className="app-panel-hint">
                    {t('app.buildFilterPanel.markers', 'Put [skip deploy] or [skip cd] in the latest commit message to skip a push. Put [skip preview] in a pull request title to skip its preview.')}
                </p>

                <div className="settings-row">
                    <div className="settings-label">
                        <label htmlFor={`build-root-${app.id}`}>
                            {t('app.buildFilterPanel.rootDir', 'Root directory')}
                        </label>
                        <span className="settings-hint">
                            {t('app.buildFilterPanel.rootDirHint', 'Only changes inside this folder of the repository deploy, e.g. apps/web.')}
                        </span>
                    </div>
                    <div className="settings-control">
                        <Input
                            id={`build-root-${app.id}`}
                            placeholder="apps/web"
                            value={rootDir}
                            onChange={(e) => setRootDir(e.target.value)}
                            disabled={saving}
                        />
                    </div>
                </div>

                <div className="settings-row">
                    <div className="settings-label">
                        <label htmlFor={`build-paths-${app.id}`}>
                            {t('app.buildFilterPanel.paths', 'Include paths')}
                        </label>
                        <span className="settings-hint">
                            {t('app.buildFilterPanel.pathsHint', 'One glob per line, e.g. services/api/**. Empty means every path.')}
                        </span>
                    </div>
                    <div className="settings-control">
                        <Textarea
                            id={`build-paths-${app.id}`}
                            rows={3}
                            value={paths}
                            onChange={(e) => setPaths(e.target.value)}
                            disabled={saving}
                        />
                    </div>
                </div>

                <div className="settings-row">
                    <div className="settings-label">
                        <label htmlFor={`build-ignored-${app.id}`}>
                            {t('app.buildFilterPanel.ignored', 'Ignored paths')}
                        </label>
                        <span className="settings-hint">
                            {t('app.buildFilterPanel.ignoredHint', 'One glob per line, e.g. docs/** or *.md. A change that only touches these does not deploy.')}
                        </span>
                    </div>
                    <div className="settings-control">
                        <Textarea
                            id={`build-ignored-${app.id}`}
                            rows={3}
                            value={ignored}
                            onChange={(e) => setIgnored(e.target.value)}
                            disabled={saving}
                        />
                    </div>
                </div>

                <div className="settings-row">
                    <div className="settings-label" />
                    <div className="settings-control">
                        <Button size="sm" onClick={handleSave} disabled={saving || !dirty}>
                            {saving ? t('common.editing.saving', 'Saving…') : t('common.actions.save', 'Save')}
                        </Button>
                    </div>
                </div>
            </div>
        </div>
    );
};

export default BuildFilterPanel;
