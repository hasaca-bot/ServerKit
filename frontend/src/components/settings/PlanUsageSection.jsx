import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import api from '../../services/api';
import { Gauge } from '@/components/ds/Gauge';
import { Pill } from '@/components/ds/Pill';

// Resource kinds in the order the plan page lists them.
const kindLabels = (t) => [
    ['apps', t('app.planUsage.kindApps', 'Services')],
    ['databases', t('app.planUsage.kindDatabases', 'Databases')],
    ['cron_jobs', t('app.planUsage.kindCronJobs', 'Cron jobs')],
    ['custom_domains', t('app.planUsage.kindCustomDomains', 'Custom domains')],
    ['previews', t('app.planUsage.kindPreviews', 'Preview environments')],
    ['servers', t('app.planUsage.kindServers', 'Servers')],
    ['mcp_keys', t('app.planUsage.kindMcpKeys', 'API / MCP keys')],
];

function limitLabel(limit, t) {
    return limit === null || limit === undefined ? t('app.planUsage.unlimited', 'Unlimited') : limit;
}

// "Plan & usage" for the signed-in user: the plan's limits next to what the
// user currently owns (GET /api/v1/plan).
const PlanUsageSection = () => {
    const { t } = useTranslation();
    const [data, setData] = useState(null);
    const [error, setError] = useState(null);

    useEffect(() => {
        let alive = true;
        api.getMyPlan()
            .then((res) => { if (alive) setData(res); })
            .catch((err) => { if (alive) setError(err.message); });
        return () => { alive = false; };
    }, []);

    return (
        <div className="settings-section plan-usage">
            <div className="section-header">
                <h2>{t('app.planUsage.title', 'Plan & usage')}</h2>
                {data && (
                    <Pill kind="cyan">
                        {data.unlimited
                            ? t('app.planUsage.admin', 'Administrator, no limits')
                            : data.plan?.name}
                    </Pill>
                )}
            </div>

            {error && <div className="alert alert-danger">{error}</div>}

            {data && (
                <>
                    <ul className="plan-usage__list">
                        {kindLabels(t).map(([kind, label]) => {
                            const used = data.usage?.[kind] ?? 0;
                            const limit = data.limits?.[kind];
                            const capped = limit !== null && limit !== undefined;
                            const pct = capped ? (limit === 0 ? (used > 0 ? 100 : 0) : (used / limit) * 100) : 0;
                            return (
                                <li key={kind} className="plan-usage__row">
                                    <span className="plan-usage__label">{label}</span>
                                    <span className="plan-usage__count">
                                        {used} / {limitLabel(limit, t)}
                                    </span>
                                    {capped && <Gauge value={pct} className="plan-usage__gauge" />}
                                </li>
                            );
                        })}
                    </ul>
                    {data.plan && (
                        <p className="plan-usage__note">
                            {t('app.planUsage.perService', 'Per service: up to {{cpu}} CPU and {{memory}} MB memory.', {
                                cpu: data.plan.max_cpu ?? '∞',
                                memory: data.plan.max_memory_mb ?? '∞',
                            })}
                            {data.plan.sleep_after_minutes
                                ? ` ${t('app.planUsage.sleeps', 'Idle services sleep after {{minutes}} minutes.', {
                                    minutes: data.plan.sleep_after_minutes,
                                })}`
                                : ''}
                        </p>
                    )}
                </>
            )}
        </div>
    );
};

export default PlanUsageSection;
