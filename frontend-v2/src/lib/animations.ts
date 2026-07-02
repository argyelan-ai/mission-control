'use client';

import { RefObject, useEffect, useRef, useState } from 'react';

/**
 * Hook to detect if an element is in the viewport (in view)
 * Triggers when at least 20% of the element is visible
 */
export function useInView<T extends HTMLElement>(
  ref: RefObject<T>,
  threshold = 0.2
): boolean {
  const [isIntersecting, setIsIntersecting] = useState(false);

  useEffect(() => {
    const element = ref.current;
    if (!element) return;

    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting || entry.intersectionRatio >= threshold) {
          setIsIntersecting(true);
          // Once visible, stop observing
          observer.disconnect();
        }
      },
      { threshold }
    );

    observer.observe(element);

    return () => observer.disconnect();
  }, [ref, threshold]);

  return isIntersecting;
}

/**
 * Hook to track scroll progress (0-1)
 * Returns both the current progress and vertical scroll position
 */
export function useScrollProgress(): { progress: number; currentY: number } {
  const [scrollProgress, setScrollProgress] = useState(0);
  const [currentY, setCurrentY] = useState(0);

  useEffect(() => {
    const handleScroll = () => {
      const totalHeight = document.documentElement.scrollHeight - window.innerHeight;
      const progress = Math.min(window.scrollY / totalHeight, 1);
      setScrollProgress(progress);
      setCurrentY(window.scrollY);
    };

    window.addEventListener('scroll', handleScroll);
    return () => window.removeEventListener('scroll', handleScroll);
  }, []);

  return { progress: scrollProgress, currentY };
}
