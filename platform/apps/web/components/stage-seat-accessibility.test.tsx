import axe from "axe-core";
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { StageSeatAccessibility } from "@/components/stage-seat-accessibility";
import type { Room } from "@/lib/types";

function room(overrides: Partial<Room> = {}): Room {
  return {
    status: "running",
    current_stage: { key: "free", name: "自由辩论", kind: "free", duration: 300, side: "aff" },
    active_speech: null,
    ...overrides,
  } as Room;
}

function FrozenStage() {
  return (
    <div className="stage-page">
      <section className="stage-arena">
        <aside className="team-column aff">
          <div className="team-title"><span>正方</span><strong>PROPOSITION</strong></div>
          {[1, 2, 3, 4].map((position) => (
            <div className="stage-seat active" key={position}>
              <span>正方{position}辩</span>
              <span className="sr-only"> · 原辩手在线 · 当前发言席位</span>
            </div>
          ))}
        </aside>
        <main><div className="subtitle-stage"><p>实时字幕</p></div></main>
        <aside className="team-column neg">
          <div className="team-title"><span>反方</span><strong>OPPOSITION</strong></div>
          <div className="stage-seat"><span>反方一辩</span><span className="sr-only"> · 系统席位</span></div>
        </aside>
      </section>
    </div>
  );
}

function renderStage(targetRoom: Room) {
  return render(<><FrozenStage /><StageSeatAccessibility room={targetRoom} /></>);
}

describe("StageSeatAccessibility", () => {
  it("describes every highlighted 4v4 free-debate seat as eligible and labels the eligible side", () => {
    const view = renderStage(room());
    expect(view.container.querySelectorAll('[data-stage-seat-a11y="seat"]')).toHaveLength(4);
    expect(view.getAllByText(/本轮可发言席位/)).toHaveLength(4);
    expect(view.container.querySelectorAll('[data-stage-seat-a11y="source"][aria-hidden="true"]')).toHaveLength(4);
    expect(Array.from(view.container.querySelectorAll('[data-stage-seat-a11y="seat"]')).every((node) => !node.textContent?.includes("当前发言席位"))).toBe(true);
    expect(view.getByText(/当前可发言阵营/)).toBeInTheDocument();
  });

  it("supports 1v1 and switches the managed semantics to the other side without leaving stale labels", () => {
    const { container, rerender } = render(
      <>
        <div className="stage-page">
          <aside className="team-column aff"><div className="team-title">正方</div><div className="stage-seat active"><span className="sr-only">当前发言席位</span></div></aside>
          <aside className="team-column neg"><div className="team-title">反方</div><div className="stage-seat active"><span className="sr-only">当前发言席位</span></div></aside>
        </div>
        <StageSeatAccessibility room={room()} />
      </>,
    );
    expect(container.querySelector('.team-column.aff [data-stage-seat-a11y="seat"]')).toHaveTextContent("本轮可发言席位");
    expect(container.querySelector(".team-column.neg .stage-seat .sr-only")).toHaveTextContent("当前发言席位");

    rerender(
      <>
        <div className="stage-page">
          <aside className="team-column aff"><div className="team-title">正方</div><div className="stage-seat active"><span className="sr-only">当前发言席位</span></div></aside>
          <aside className="team-column neg"><div className="team-title">反方</div><div className="stage-seat active"><span className="sr-only">当前发言席位</span></div></aside>
        </div>
        <StageSeatAccessibility room={room({ current_stage: { key: "free", name: "自由辩论", kind: "free", duration: 300, side: "neg" } })} />
      </>,
    );
    expect(container.querySelector(".team-column.aff .stage-seat .sr-only")).toHaveTextContent("当前发言席位");
    expect(container.querySelector('.team-column.neg [data-stage-seat-a11y="seat"]')).toHaveTextContent("本轮可发言席位");
    expect(container.querySelectorAll('[data-stage-seat-a11y="side"]')).toHaveLength(1);
  });

  it("keeps the unique current-speaker wording during fixed stages and active free-debate speeches", () => {
    const { queryByText, rerender } = renderStage(room({
      current_stage: { key: "aff_1", name: "正方一辩立论", kind: "speech", duration: 180, seat: "aff_1" },
    }));
    expect(queryByText(/本轮可发言席位/)).not.toBeInTheDocument();
    expect(queryByText(/当前可发言阵营/)).not.toBeInTheDocument();

    rerender(<><FrozenStage /><StageSeatAccessibility room={room({ active_speech: { id: "speech", seat_key: "aff_1", speaker_type: "human", status: "speaking", content: "" } })} /></>);
    expect(queryByText(/本轮可发言席位/)).not.toBeInTheDocument();
    expect(queryByText(/当前可发言阵营/)).not.toBeInTheDocument();
  });

  it("announces paused free-debate eligibility without claiming anyone can currently speak", () => {
    const view = renderStage(room({ status: "paused" }));
    expect(view.getAllByText(/本轮可发言席位，比赛已暂停/)).toHaveLength(4);
    expect(view.getByText(/本轮轮到的阵营，比赛已暂停/)).toBeInTheDocument();
  });

  it("reapplies the label on a new room snapshot without overwriting updated connection text", () => {
    function SnapshotStage({ connected }: { connected: boolean }) {
      return (
        <div className="stage-page">
          <aside className="team-column aff">
            <div className="team-title">正方</div>
            <div className="stage-seat active">
              <span className="sr-only">{connected ? "原辩手在线" : "原辩手离线"} · 当前发言席位</span>
            </div>
          </aside>
        </div>
      );
    }
    const view = render(<><SnapshotStage connected /><StageSeatAccessibility room={room({ seq: 1 })} /></>);
    expect(view.getByText(/原辩手在线 · 本轮可发言席位/)).toBeInTheDocument();

    view.rerender(<><SnapshotStage connected={false} /><StageSeatAccessibility room={room({ seq: 2 })} /></>);
    expect(view.getByText(/原辩手离线 · 本轮可发言席位/)).toBeInTheDocument();
    expect(view.queryByText(/原辩手在线/)).not.toBeInTheDocument();
  });

  it("cleans managed text on stage transition and leaves the one-line subtitle out of keyboard order", () => {
    const view = renderStage(room());
    const firstSeatSource = view.container.querySelector<HTMLElement>('[data-stage-seat-a11y="source"]');
    const subtitle = view.container.querySelector<HTMLElement>(".subtitle-stage p");
    expect(subtitle).not.toHaveAttribute("tabindex");
    expect(firstSeatSource).toHaveTextContent("当前发言席位");
    expect(firstSeatSource).toHaveAttribute("aria-hidden", "true");
    expect(view.container.querySelectorAll('[data-stage-seat-a11y]')).toHaveLength(9);

    view.rerender(<><FrozenStage /><StageSeatAccessibility room={room({ current_stage: { key: "aff_1", name: "正方一辩立论", kind: "speech", duration: 180, seat: "aff_1" } })} /></>);
    expect(firstSeatSource).toHaveTextContent("当前发言席位");
    expect(firstSeatSource).not.toHaveAttribute("aria-hidden");
    expect(view.container.querySelectorAll('[data-stage-seat-a11y]')).toHaveLength(0);
    expect(view.container.querySelector(".subtitle-stage p")).not.toHaveAttribute("tabindex");

    const currentSubtitle = view.container.querySelector<HTMLElement>(".subtitle-stage p");
    view.unmount();
    expect(currentSubtitle).not.toHaveAttribute("tabindex");
  });

  it("introduces no axe violations or extra keyboard stops", async () => {
    const view = renderStage(room());
    expect(view.container.querySelectorAll('[data-stage-seat-a11y][tabindex]')).toHaveLength(0);
    expect(view.container.querySelectorAll('[tabindex="0"]')).toHaveLength(0);
    expect((await axe.run(view.container, { rules: { "color-contrast": { enabled: false } } })).violations).toEqual([]);
  });
});
