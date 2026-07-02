"use client";

export function AmbientBackground() {
  return (
    <div
      aria-hidden
      className="pointer-events-none fixed inset-0 z-0 overflow-hidden"
    >
      {/* Static ambient glows — MC Teal, very faint. Radial-gradients instead
          of blurred solid circles: same soft look, no filter compositing cost. */}
      <div
        className="absolute"
        style={{
          background: "radial-gradient(circle, rgba(15,163,163,0.06) 0%, rgba(15,163,163,0) 65%)",
          width: 800,
          height: 800,
          left: "15%",
          top: "20%",
          transform: "translate(-50%, -50%)",
        }}
      />
      <div
        className="absolute"
        style={{
          background: "radial-gradient(circle, rgba(15,163,163,0.04) 0%, rgba(15,163,163,0) 65%)",
          width: 700,
          height: 700,
          left: "70%",
          top: "60%",
          transform: "translate(-50%, -50%)",
        }}
      />
      <div
        className="absolute"
        style={{
          background: "radial-gradient(circle, rgba(255,255,255,0.025) 0%, rgba(255,255,255,0) 65%)",
          width: 750,
          height: 750,
          left: "50%",
          top: "80%",
          transform: "translate(-50%, -50%)",
        }}
      />

      {/* Grain overlay */}
      <div
        className="fixed inset-0 opacity-[0.03]"
        style={{
          backgroundImage: `url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E")`,
          backgroundRepeat: "repeat",
          backgroundSize: "128px 128px",
        }}
      />
    </div>
  );
}
