import { create } from 'zustand';
import { bundleApi, type BundleCreatePayload, type BundleScopePayload } from '../api/bundles';
import type { BundlePreview, JointBundle } from '../types/models';
import { errorMessage } from '../utils/format';

interface BundleState {
  items: JointBundle[];
  preview: BundlePreview | null;
  previewing: boolean;
  loading: boolean;
  busyId: string | null;
  error: string | null;
  load: () => Promise<void>;
  runPreview: (payload: BundleScopePayload & { dataset_version?: string }) => Promise<boolean>;
  create: (payload: BundleCreatePayload) => Promise<JointBundle | null>;
  approve: (bundleId: string, note: string) => Promise<boolean>;
  reject: (bundleId: string, note: string) => Promise<boolean>;
  retry: (bundleId: string) => Promise<boolean>;
  clearPreview: () => void;
}

export const useBundleStore = create<BundleState>((set, get) => ({
  items: [],
  preview: null,
  previewing: false,
  loading: false,
  busyId: null,
  error: null,
  load: async () => {
    set({ loading: true, error: null });
    try {
      const response = await bundleApi.list();
      set({ items: response.items, loading: false });
    } catch (error) {
      set({ loading: false, error: errorMessage(error) });
    }
  },
  runPreview: async (payload) => {
    set({ previewing: true, error: null });
    try {
      const preview = await bundleApi.preview(payload);
      set({ preview, previewing: false });
      return true;
    } catch (error) {
      set({ previewing: false, error: errorMessage(error) });
      return false;
    }
  },
  create: async (payload) => {
    set({ busyId: 'creating', error: null });
    try {
      const response = await bundleApi.create(payload);
      await get().load();
      set({ busyId: null, preview: null });
      return response.bundle;
    } catch (error) {
      set({ busyId: null, error: errorMessage(error) });
      return null;
    }
  },
  approve: async (bundleId, note) => {
    set({ busyId: bundleId, error: null });
    try {
      await bundleApi.approve(bundleId, note);
      await get().load();
      set({ busyId: null });
      return true;
    } catch (error) {
      set({ busyId: null, error: errorMessage(error) });
      return false;
    }
  },
  reject: async (bundleId, note) => {
    set({ busyId: bundleId, error: null });
    try {
      await bundleApi.reject(bundleId, note);
      await get().load();
      set({ busyId: null });
      return true;
    } catch (error) {
      set({ busyId: null, error: errorMessage(error) });
      return false;
    }
  },
  retry: async (bundleId) => {
    set({ busyId: bundleId, error: null });
    try {
      await bundleApi.retry(bundleId);
      await get().load();
      set({ busyId: null });
      return true;
    } catch (error) {
      set({ busyId: null, error: errorMessage(error) });
      return false;
    }
  },
  clearPreview: () => set({ preview: null }),
}));
