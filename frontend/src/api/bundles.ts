import { api } from './client';
import type { BundlePreview, JointBundle } from '../types/models';
import type { ListResponse } from '../types/api';

export interface BundleScopePayload {
  patient_ids?: string[];
  patient_id_start?: string;
  patient_id_end?: string;
  all_patients?: boolean;
  date_granularity: 'year' | 'month';
}

export interface BundleCreatePayload extends BundleScopePayload {
  purpose: string;
  dataset_version?: string;
}

export const bundleApi = {
  list: () => api.get<ListResponse<JointBundle>>('/bundles'),
  preview: (payload: BundleScopePayload & { dataset_version?: string }) =>
    api.post<BundlePreview>('/bundles/preview', payload),
  create: (payload: BundleCreatePayload) =>
    api.post<{ bundle: JointBundle }>('/bundles', payload),
  detail: (bundleId: string) => api.get<{ bundle: JointBundle }>(`/bundles/${bundleId}`),
  approve: (bundleId: string, note: string) =>
    api.post<{ bundle: JointBundle }>(`/bundles/${bundleId}/approve`, { note }),
  reject: (bundleId: string, note: string) =>
    api.post<{ bundle: JointBundle }>(`/bundles/${bundleId}/reject`, { note }),
  retry: (bundleId: string) => api.post<{ bundle: JointBundle }>(`/bundles/${bundleId}/retry`),
  downloadUrl: (bundleId: string) => api.downloadPath(`/bundles/${bundleId}/download`),
  downloadTableUrl: (bundleId: string, tableName: string) =>
    api.downloadPath(`/bundles/${bundleId}/download/${tableName}`),
};
