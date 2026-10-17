import { useState, useEffect, useCallback } from 'react';
import api from '../services/api';

export function useDeployments(appId) {
    const [deployments, setDeployments] = useState([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);

    const load = useCallback(async () => {
        if (!appId) return;
        try {
            setLoading(true);
            setError(null);

            // Fetch from both deploy systems in parallel
            const [buildRes, deployRes] = await Promise.allSettled([
                api.getDeployments(appId, 20),
                api.getDeploymentHistory(appId, 20),
            ]);

            const buildDeploys = buildRes.status === 'fulfilled'
                ? (buildRes.value.deployments || []).map(d => ({
                    id: d.id || d.version,
                    type: 'build',
                    status: d.status || 'success',
                    version: d.version,
                    commitSha: d.commit_sha || d.git_commit,
                    commitMessage: d.commit_message || d.description,
                    branch: d.branch,
                    trigger: d.trigger || 'manual',
                    duration: d.duration,
                    timestamp: d.created_at || d.timestamp,
                    logs: d.logs,
                }))
                : [];

            const gitDeploys = deployRes.status === 'fulfilled'
                ? (deployRes.value.deployments || []).map(d => ({
                    id: d.id || `deploy-${d.timestamp}`,
                    type: 'deploy',
                    status: d.status || 'success',
                    version: d.version,
                    commitSha: d.commit_sha || d.git_commit,
                    commitMessage: d.commit_message || d.description,
                    branch: d.branch,
                    trigger: d.trigger || d.triggered_by || (d.auto ? 'push' : 'manual'),
                    duration: d.duration,
                    timestamp: d.created_at || d.timestamp,
                    logs: d.logs,
                    // Why a push did not deploy (status 'skipped').
                    reason: d.status === 'skipped' ? d.error_message : undefined,
                }))
                : [];

            // Merge and deduplicate by timestamp proximity, sort newest first
            const all = [...buildDeploys, ...gitDeploys];
            all.sort((a, b) => new Date(b.timestamp) - new Date(a.timestamp));

            setDeployments(all);
        } catch (err) {
            setError(err.message || 'Failed to load deployments');
        } finally {
            setLoading(false);
        }
    }, [appId]);

    useEffect(() => {
        load();
    }, [load]);

    return { deployments, loading, error, reload: load };
}

export default useDeployments;
