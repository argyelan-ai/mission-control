/**
 * AvatarStack — vitest.
 *
 * Abgedeckt: wie viele Kreise wirklich gerendert werden, der „+N"-Überlauf ab
 * `max`, das Screenreader-Label (die Icons selbst sind aria-hidden) und die
 * leere Liste, die gar nichts rendern darf.
 */
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { AvatarStack } from "./AvatarStack";

type Member = { id: string; emoji: string | null; name: string };

function mkMembers(names: string[]): Member[] {
  return names.map((name, i) => ({ id: `m-${i}`, emoji: null, name }));
}

describe("AvatarStack", () => {
  it("renders one circle per member when the list fits within max", () => {
    render(<AvatarStack members={mkMembers(["Rex", "Cody", "Sparky"])} />);
    expect(screen.getAllByTestId("avatar-stack-item")).toHaveLength(3);
    expect(screen.queryByTestId("avatar-stack-more")).not.toBeInTheDocument();
  });

  it('shows "+2" as a trailing circle when 5 members exceed the default max of 3', () => {
    render(<AvatarStack members={mkMembers(["Rex", "Cody", "Sparky", "Hermes", "Mia"])} />);
    expect(screen.getAllByTestId("avatar-stack-item")).toHaveLength(3);
    expect(screen.getByText("+2")).toBeInTheDocument();
  });

  it("honours a custom max instead of the default", () => {
    render(<AvatarStack members={mkMembers(["Rex", "Cody", "Sparky", "Hermes", "Mia"])} max={4} />);
    expect(screen.getAllByTestId("avatar-stack-item")).toHaveLength(4);
    expect(screen.getByText("+1")).toBeInTheDocument();
  });

  it("names every member — including the hidden ones — for screen readers", () => {
    render(<AvatarStack members={mkMembers(["Rex", "Cody", "Sparky", "Hermes"])} />);
    expect(screen.getByRole("img", { name: "Rex, Cody, Sparky, Hermes" })).toBeInTheDocument();
  });

  it("renders nothing at all for an empty member list", () => {
    const { container } = render(<AvatarStack members={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("sizes the circles from the size prop", () => {
    render(<AvatarStack members={mkMembers(["Rex"])} size={28} />);
    const circle = screen.getByTestId("avatar-stack-item");
    expect(circle).toHaveStyle({ width: "28px", height: "28px" });
  });
});
