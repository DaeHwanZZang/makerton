// 선 아이콘 (stroke = currentColor)
type P = { size?: number };

const base = (size: number) => ({
  width: size,
  height: size,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.6,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
});

export const IconBuilding = ({ size = 30 }: P) => (
  <svg {...base(size)}>
    <rect x="3.5" y="4" width="17" height="16" rx="1" />
    <path d="M8 4v16M12 4v16M16 4v16" />
  </svg>
);

export const IconSizeS = ({ size = 32 }: P) => (
  <svg {...base(size)}>
    <rect x="4" y="4" width="16" height="16" rx="1" />
    <rect x="8" y="8" width="8" height="8" fill="currentColor" stroke="none" opacity="0.55" />
  </svg>
);

export const IconSizeM = ({ size = 32 }: P) => (
  <svg {...base(size)}>
    <rect x="4" y="4" width="16" height="16" rx="1" />
    <path d="M4 8.5h16M4 12h16M4 15.5h16" />
  </svg>
);

export const IconSizeL = ({ size = 32 }: P) => (
  <svg {...base(size)}>
    <rect x="3" y="4" width="18" height="16" rx="1" />
    <path d="M7.5 4v16M12 4v16M16.5 4v16" />
  </svg>
);

export const IconPackage = ({ size = 26 }: P) => (
  <svg {...base(size)}>
    <path d="M12 3 20 7.5v9L12 21l-8-4.5v-9L12 3Z" />
    <path d="M4 7.5 12 12l8-4.5M12 12v9M8 5.3l8 4.5" />
  </svg>
);

export const IconCheck = ({ size = 18 }: P) => (
  <svg {...base(size)} strokeWidth={2.4}>
    <path d="m5 12.5 4.5 4.5L19 7.5" />
  </svg>
);

export const IconArrowRight = ({ size = 20 }: P) => (
  <svg {...base(size)} strokeWidth={2.2}>
    <path d="M5 12h14M13 6l6 6-6 6" />
  </svg>
);

export const IconArrowLeft = ({ size = 20 }: P) => (
  <svg {...base(size)} strokeWidth={2.2}>
    <path d="M19 12H5M11 6l-6 6 6 6" />
  </svg>
);

export const IconInfo = ({ size = 18 }: P) => (
  <svg {...base(size)}>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 11v5M12 8h.01" />
  </svg>
);

export const IconArrowDown = ({ size = 28 }: P) => (
  <svg {...base(size)} strokeWidth={2.4}>
    <path d="M12 4v15M6 13l6 6 6-6" />
  </svg>
);

export const sizeIcon = (key: string, size?: number) =>
  key === "S" ? <IconSizeS size={size} /> : key === "M" ? <IconSizeM size={size} /> : <IconSizeL size={size} />;
