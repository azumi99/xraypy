import { create } from "zustand";
import { persist } from "zustand/middleware";

export const useAwbDisplayStore = create(
  persist(
    (set) => ({
      grouped: false,
      setGrouped: (grouped) => set({ grouped }),
    }),
    { name: "awb-display-mode" },
  ),
);
