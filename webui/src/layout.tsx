import type { HTMLAttributes, ReactNode } from "react";

function classes(...values: Array<string | false | null | undefined>): string {
  return values.filter(Boolean).join(" ");
}

type DivProps = HTMLAttributes<HTMLDivElement>;

export function PageFrame({ className, ...props }: DivProps): React.JSX.Element {
  return <div className={classes("page-frame", className)} {...props} />;
}

export function PanelStack({ className, ...props }: DivProps): React.JSX.Element {
  return <div className={classes("panel-stack", className)} {...props} />;
}

export function PanelGrid({ className, ...props }: DivProps): React.JSX.Element {
  return <div className={classes("panel-grid", className)} {...props} />;
}

export function ScrollRegion({ className, ...props }: DivProps): React.JSX.Element {
  return <div className={classes("scroll-region", className)} {...props} />;
}

export function StickyActions({ className, ...props }: DivProps): React.JSX.Element {
  return <div className={classes("sticky-actions", className)} {...props} />;
}

export function SplitWorkspace({
  primary,
  secondary,
  className,
  primaryClassName,
  secondaryClassName,
}: {
  primary: ReactNode;
  secondary: ReactNode;
  className?: string;
  primaryClassName?: string;
  secondaryClassName?: string;
}): React.JSX.Element {
  return (
    <div className={classes("split-workspace", className)}>
      <div className={classes("split-workspace-primary", primaryClassName)}>{primary}</div>
      <aside className={classes("split-workspace-secondary", secondaryClassName)}>{secondary}</aside>
    </div>
  );
}

export type SectionNavItem = {
  id: string;
  label: string;
  hint?: string;
};

export function SectionNav({
  items,
  title = "本页章节",
}: {
  items: SectionNavItem[];
  title?: string;
}): React.JSX.Element {
  const jumpTo = (id: string): void => {
    const target = document.getElementById(id);
    if (!target) return;
    target.scrollIntoView({ behavior: "smooth", block: "start" });
    if (typeof target.focus === "function") {
      window.setTimeout(() => target.focus({ preventScroll: true }), 240);
    }
  };

  return (
    <nav className="section-nav" aria-label={title}>
      <div className="section-nav-title">{title}</div>
      <div className="section-nav-list">
        {items.map((item, index) => (
          <button
            key={item.id}
            type="button"
            className="section-nav-item"
            onClick={() => jumpTo(item.id)}
          >
            <span className="section-nav-index">{String(index + 1).padStart(2, "0")}</span>
            <span className="section-nav-copy">
              <strong>{item.label}</strong>
              {item.hint && <small>{item.hint}</small>}
            </span>
          </button>
        ))}
      </div>
    </nav>
  );
}
