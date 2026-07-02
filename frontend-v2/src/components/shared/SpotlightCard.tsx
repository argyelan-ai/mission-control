"use client";

import { forwardRef } from "react";
import { cn } from "@/lib/utils";
import { useSpotlight } from "@/hooks/useSpotlight";

const SPOTLIGHT_STYLE = (
  <style>{`
    .spotlight-card::after {
      content: '';
      position: absolute;
      inset: 0;
      border-radius: inherit;
      opacity: 0;
      transition: opacity 0.3s ease;
      pointer-events: none;
      background: radial-gradient(
        400px circle at var(--mouse-x, 50%) var(--mouse-y, 50%),
        rgba(255, 255, 255, 0.04),
        transparent 60%
      );
    }
    .spotlight-card:hover::after {
      opacity: 1;
    }
  `}</style>
);

type DivProps = { as?: "div" } & React.HTMLAttributes<HTMLDivElement>;
type ButtonProps = { as: "button" } & React.ButtonHTMLAttributes<HTMLButtonElement>;
type SpotlightCardProps = DivProps | ButtonProps;

export const SpotlightCard = forwardRef<HTMLElement, SpotlightCardProps>(
  (props, _ref) => {
    const { ref, onMouseMove } = useSpotlight<HTMLElement>();
    const className = cn("spotlight-card relative overflow-hidden", props.className);

    if (props.as === "button") {
      const { as: _as, children, className: _cn, ...rest } = props;
      return (
        <button
          ref={ref as React.Ref<HTMLButtonElement>}
          onMouseMove={onMouseMove}
          className={className}
          {...rest}
        >
          {children}
          {SPOTLIGHT_STYLE}
        </button>
      );
    }

    const { as: _as, children, className: _cn, ...rest } = props;
    return (
      <div
        ref={ref as React.Ref<HTMLDivElement>}
        onMouseMove={onMouseMove}
        className={className}
        {...rest}
      >
        {children}
        {SPOTLIGHT_STYLE}
      </div>
    );
  }
);

SpotlightCard.displayName = "SpotlightCard";
