'use client';

import React, { ButtonHTMLAttributes } from 'react';
import { cn } from '@/lib/utils';
import { C } from '@/lib/colors';

type ButtonVariant = 'primary' | 'secondary';
type ButtonSize = 'sm' | 'md' | 'lg';

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  isLoading?: boolean;
}

const variantStyles = {
  primary:
    'text-white active:scale-[0.98]',
  secondary:
    'bg-transparent text-white active:scale-[0.98]',
};

const sizeStyles = {
  sm: 'px-3 py-1.5 text-sm',
  md: 'px-5 py-2.5 text-base',
  lg: 'px-6 py-3 text-lg',
};

export function Button({
  children,
  className,
  variant = 'primary',
  size = 'md',
  isLoading,
  disabled,
  style,
  ...props
}: ButtonProps) {
  const variantInlineStyle =
    variant === 'primary'
      ? {
          backgroundColor: C.accent,
          boxShadow: 'none',
        }
      : {
          border: `1px solid ${C.textSecondary}`,
          backgroundColor: 'transparent',
        };

  const hoverClass =
    variant === 'primary'
      ? 'hover:brightness-110'
      : 'hover:border-[var(--color-accent)] hover:text-[var(--color-accent)]';

  return (
    <button
      className={cn(
        'inline-flex items-center justify-center font-medium transition-all duration-300 ease-[cubic-bezier(0.2,0.8,0.2,1)] rounded-lg disabled:opacity-50 disabled:cursor-not-allowed',
        'focus-visible:outline-none',
        variantStyles[variant],
        hoverClass,
        sizeStyles[size],
        isLoading ? 'pointer-events-none' : '',
        className
      )}
      style={{ ...variantInlineStyle, ...style }}
      disabled={isLoading || disabled}
      {...props}
    >
      {isLoading && (
        <svg
          className="animate-spin -ml-1 mr-2 h-4 w-4"
          xmlns="http://www.w3.org/2000/svg"
          fill="none"
          viewBox="0 0 24 24"
          style={{ color: C.accentHover, borderTopColor: C.accent }}
        >
          <circle
            className="opacity-25"
            cx="12"
            cy="12"
            r="10"
            stroke="currentColor"
            strokeWidth="4"
          />
          <path
            className="opacity-75"
            fill="currentColor"
            d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"
          />
        </svg>
      )}
      {children}
    </button>
  );
}
