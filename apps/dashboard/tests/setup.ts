import '@testing-library/jest-dom/vitest';
import { vi } from 'vitest';

// Page-level RTL tests render page/component subtrees directly, with no mounted Next.js app
// router. useRouter() (used by ReviewCard to refresh after a decision) needs a stub; other
// exports (notFound, redirect, usePathname, ...) stay real.
vi.mock('next/navigation', async (importOriginal) => {
  const actual = await importOriginal<typeof import('next/navigation')>();
  return {
    ...actual,
    useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn(), back: vi.fn(), forward: vi.fn(), prefetch: vi.fn() }),
  };
});
