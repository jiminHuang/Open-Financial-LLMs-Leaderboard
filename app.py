import json
import os
from datetime import datetime, timezone


import gradio as gr
import numpy as np
import pandas as pd
from apscheduler.schedulers.background import BackgroundScheduler
from huggingface_hub import HfApi
from transformers import AutoConfig

from src.auto_leaderboard.get_model_metadata import apply_metadata
from src.assets.text_content import *
from src.elo_leaderboard.load_results import get_elo_plots, get_elo_results_dicts
from src.auto_leaderboard.load_results import get_eval_results_dicts, make_clickable_model
from src.assets.hardcoded_evals import gpt4_values, gpt35_values, baseline
from src.assets.css_html_js import custom_css, get_window_url_params
from src.utils_display import AutoEvalColumn, EvalQueueColumn, EloEvalColumn, fields, styled_error, styled_warning, styled_message
from src.init import load_all_info_from_hub

# clone / pull the lmeh eval data
H4_TOKEN = os.environ.get("H4_TOKEN", None)
LMEH_REPO = "HuggingFaceH4/lmeh_evaluations"
HUMAN_EVAL_REPO = "HuggingFaceH4/scale-human-eval"
GPT_4_EVAL_REPO = "HuggingFaceH4/open_llm_leaderboard_oai_evals"
IS_PUBLIC = bool(os.environ.get("IS_PUBLIC", True))
ADD_PLOTS = False

EVAL_REQUESTS_PATH = "auto_evals/eval_requests"

api = HfApi()


def restart_space():
    api.restart_space(
        repo_id="HuggingFaceH4/open_llm_leaderboard", token=H4_TOKEN
    )

auto_eval_repo, human_eval_repo, gpt_4_eval_repo, requested_models = load_all_info_from_hub(LMEH_REPO, HUMAN_EVAL_REPO, GPT_4_EVAL_REPO)

COLS = [c.name for c in fields(AutoEvalColumn) if not c.hidden]
TYPES = [c.type for c in fields(AutoEvalColumn) if not c.hidden]
COLS_LITE = [c.name for c in fields(AutoEvalColumn) if c.displayed_by_default and not c.hidden]
TYPES_LITE = [c.type for c in fields(AutoEvalColumn) if c.displayed_by_default and not c.hidden]

if not IS_PUBLIC:
    COLS.insert(2, AutoEvalColumn.is_8bit.name)
    TYPES.insert(2, AutoEvalColumn.is_8bit.type)

EVAL_COLS = [c.name for c in fields(EvalQueueColumn)]
EVAL_TYPES = [c.type for c in fields(EvalQueueColumn)]

BENCHMARK_COLS = [c.name for c in [AutoEvalColumn.arc, AutoEvalColumn.hellaswag, AutoEvalColumn.mmlu, AutoEvalColumn.truthfulqa]]

ELO_COLS = [c.name for c in fields(EloEvalColumn)]
ELO_TYPES = [c.type for c in fields(EloEvalColumn)]
ELO_SORT_COL = EloEvalColumn.gpt4.name


def has_no_nan_values(df, columns):
    return df[columns].notna().all(axis=1)


def has_nan_values(df, columns):
    return df[columns].isna().any(axis=1)


def get_leaderboard_df():
    if auto_eval_repo:
        print("Pulling evaluation results for the leaderboard.")
        auto_eval_repo.git_pull()

    all_data = get_eval_results_dicts(IS_PUBLIC)

    if not IS_PUBLIC:
        all_data.append(gpt4_values)
        all_data.append(gpt35_values)

    all_data.append(baseline)
    apply_metadata(all_data)  # Populate model type based on known hardcoded values in `metadata.py`

    df = pd.DataFrame.from_records(all_data)
    df = df.sort_values(by=[AutoEvalColumn.average.name], ascending=False)
    df = df[COLS]

    # filter out if any of the benchmarks have not been produced
    df = df[has_no_nan_values(df, BENCHMARK_COLS)]
    return df


def get_evaluation_queue_df():
    # todo @saylortwift: replace the repo by the one you created for the eval queue
    if auto_eval_repo:
        print("Pulling changes for the evaluation queue.")
        auto_eval_repo.git_pull()

    entries = [
        entry
        for entry in os.listdir(EVAL_REQUESTS_PATH)
        if not entry.startswith(".")
    ]
    all_evals = []

    for entry in entries:
        if ".json" in entry:
            file_path = os.path.join(EVAL_REQUESTS_PATH, entry)
            with open(file_path) as fp:
                data = json.load(fp)

            data["# params"] = "unknown"
            data["model"] = make_clickable_model(data["model"])
            data["revision"] = data.get("revision", "main")

            all_evals.append(data)
        else:
            # this is a folder
            sub_entries = [
                e
                for e in os.listdir(f"{EVAL_REQUESTS_PATH}/{entry}")
                if not e.startswith(".")
            ]
            for sub_entry in sub_entries:
                file_path = os.path.join(EVAL_REQUESTS_PATH, entry, sub_entry)
                with open(file_path) as fp:
                    data = json.load(fp)

                # data["# params"] = get_n_params(data["model"])
                data["model"] = make_clickable_model(data["model"])
                all_evals.append(data)

    pending_list = [e for e in all_evals if e["status"] == "PENDING"]
    running_list = [e for e in all_evals if e["status"] == "RUNNING"]
    finished_list = [e for e in all_evals if e["status"] == "FINISHED"]
    df_pending = pd.DataFrame.from_records(pending_list)
    df_running = pd.DataFrame.from_records(running_list)
    df_finished = pd.DataFrame.from_records(finished_list)
    return df_finished[EVAL_COLS], df_running[EVAL_COLS], df_pending[EVAL_COLS]


def get_elo_leaderboard(df_instruct, df_code_instruct, tie_allowed=False):
    if human_eval_repo:
        print("Pulling human_eval_repo changes")
        human_eval_repo.git_pull()

    all_data = get_elo_results_dicts(df_instruct, df_code_instruct, tie_allowed)
    dataframe = pd.DataFrame.from_records(all_data)
    dataframe = dataframe.sort_values(by=ELO_SORT_COL, ascending=False)
    dataframe = dataframe[ELO_COLS]
    return dataframe


def get_elo_elements():
    df_instruct = pd.read_json("human_evals/without_code.json")
    df_code_instruct = pd.read_json("human_evals/with_code.json")

    elo_leaderboard = get_elo_leaderboard(
        df_instruct, df_code_instruct, tie_allowed=False
    )
    elo_leaderboard_with_tie_allowed = get_elo_leaderboard(
        df_instruct, df_code_instruct, tie_allowed=True
    )
    plot_1, plot_2, plot_3, plot_4 = get_elo_plots(
        df_instruct, df_code_instruct, tie_allowed=False
    )

    return (
        elo_leaderboard,
        elo_leaderboard_with_tie_allowed,
        plot_1,
        plot_2,
        plot_3,
        plot_4,
    )


original_df = get_leaderboard_df()
leaderboard_df = original_df.copy()
(
    finished_eval_queue_df,
    running_eval_queue_df,
    pending_eval_queue_df,
) = get_evaluation_queue_df()
(
    elo_leaderboard,
    elo_leaderboard_with_tie_allowed,
    plot_1,
    plot_2,
    plot_3,
    plot_4,
) = get_elo_elements()


def is_model_on_hub(model_name, revision) -> bool:
    try:
        AutoConfig.from_pretrained(model_name, revision=revision)
        return True, None
    
    except ValueError as e:
        return False, "needs to be launched with `trust_remote_code=True`. For safety reason, we do not allow these models to be automatically submitted to the leaderboard."

    except Exception as e:
        print("Could not get the model config from the hub.: \n", e)
        return False, "was not found on hub!"


def add_new_eval(
    model: str,
    base_model: str,
    revision: str,
    is_8_bit_eval: bool,
    private: bool,
    is_delta_weight: bool,
):
    current_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # check the model actually exists before adding the eval
    if revision == "":
        revision = "main"

    if is_delta_weight: 
        base_model_on_hub, error = is_model_on_hub(base_model, revision)
        if not base_model_on_hub:
            return styled_error(f'Base model "{base_model}" {error}')

    model_on_hub, error = is_model_on_hub(model, revision)
    if not model_on_hub:
        return styled_error(f'Model "{model}" {error}')

    print("adding new eval")

    eval_entry = {
        "model": model,
        "base_model": base_model,
        "revision": revision,
        "private": private,
        "8bit_eval": is_8_bit_eval,
        "is_delta_weight": is_delta_weight,
        "status": "PENDING",
        "submitted_time": current_time,
    }

    user_name = ""
    model_path = model
    if "/" in model:
        user_name = model.split("/")[0]
        model_path = model.split("/")[1]

    OUT_DIR = f"{EVAL_REQUESTS_PATH}/{user_name}"
    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = f"{OUT_DIR}/{model_path}_eval_request_{private}_{is_8_bit_eval}_{is_delta_weight}.json"

    # Check for duplicate submission
    if out_path.split("eval_requests/")[1].lower() in requested_models:
        return styled_warning("This model has been already submitted.")

    with open(out_path, "w") as f:
        f.write(json.dumps(eval_entry))

    api.upload_file(
        path_or_fileobj=out_path,
        path_in_repo=out_path,
        repo_id=LMEH_REPO,
        token=H4_TOKEN,
        repo_type="dataset",
    )

    return styled_message("Your request has been submitted to the evaluation queue!")


def refresh():
    leaderboard_df = get_leaderboard_df()
    (
        finished_eval_queue_df,
        running_eval_queue_df,
        pending_eval_queue_df,
    ) = get_evaluation_queue_df()
    return (
        leaderboard_df,
        finished_eval_queue_df,
        running_eval_queue_df,
        pending_eval_queue_df,
    )


def search_table(df, query):
    filtered_df = df[df[AutoEvalColumn.dummy.name].str.contains(query, case=False)]
    return filtered_df


def change_tab(query_param):
    query_param = query_param.replace("'", '"')
    query_param = json.loads(query_param)

    if (
        isinstance(query_param, dict)
        and "tab" in query_param
        and query_param["tab"] == "evaluation"
    ):
        return gr.Tabs.update(selected=1)
    else:
        return gr.Tabs.update(selected=0)


demo = gr.Blocks(css=custom_css)
with demo:
    gr.HTML(TITLE)
    with gr.Row():
        gr.Markdown(INTRODUCTION_TEXT, elem_classes="markdown-text")

    with gr.Row():
        with gr.Column():
            with gr.Accordion("📙 Citation", open=False):
                citation_button = gr.Textbox(
                    value=CITATION_BUTTON_TEXT,
                    label=CITATION_BUTTON_LABEL,
                    elem_id="citation-button",
                ).style(show_copy_button=True)
        with gr.Column():
            with gr.Accordion("✨ CHANGELOG", open=False):
                changelog = gr.Markdown(CHANGELOG_TEXT, elem_id="changelog-text")

    with gr.Tabs(elem_classes="tab-buttons") as tabs:
        with gr.TabItem("📊 LLM Benchmarks", elem_id="llm-benchmark-tab-table", id=0):
            with gr.Column():
                gr.Markdown(LLM_BENCHMARKS_TEXT, elem_classes="markdown-text")
                with gr.Box(elem_id="search-bar-table-box"):
                    search_bar = gr.Textbox(
                        placeholder="🔍 Search your model and press ENTER...",
                        show_label=False,
                        elem_id="search-bar",
                    )
                    with gr.Tabs(elem_classes="tab-buttons"):
                        with gr.TabItem("Light View"):
                            leaderboard_table_lite = gr.components.Dataframe(
                                value=leaderboard_df[COLS_LITE],
                                headers=COLS_LITE,
                                datatype=TYPES_LITE,
                                max_rows=None,
                                elem_id="leaderboard-table-lite",
                            )
                        with gr.TabItem("Extended Model View"):
                            leaderboard_table = gr.components.Dataframe(
                                value=leaderboard_df,
                                headers=COLS,
                                datatype=TYPES,
                                max_rows=None,
                                elem_id="leaderboard-table",
                            )

                    # Dummy leaderboard for handling the case when the user uses backspace key
                    hidden_leaderboard_table_for_search = gr.components.Dataframe(
                        value=original_df,
                        headers=COLS,
                        datatype=TYPES,
                        max_rows=None,
                        visible=False,
                    )
                    search_bar.submit(
                        search_table,
                        [hidden_leaderboard_table_for_search, search_bar],
                        leaderboard_table,
                    )

                    # Dummy leaderboard for handling the case when the user uses backspace key
                    hidden_leaderboard_table_for_search_lite = gr.components.Dataframe(
                        value=original_df[COLS_LITE],
                        headers=COLS_LITE,
                        datatype=TYPES_LITE,
                        max_rows=None,
                        visible=False,
                    )
                    search_bar.submit(
                        search_table,
                        [hidden_leaderboard_table_for_search_lite, search_bar],
                        leaderboard_table_lite,
                    )

                with gr.Row():
                    gr.Markdown(EVALUATION_QUEUE_TEXT, elem_classes="markdown-text")

                with gr.Accordion("✅ Finished Evaluations", open=False):
                    with gr.Row():
                        finished_eval_table = gr.components.Dataframe(
                            value=finished_eval_queue_df,
                            headers=EVAL_COLS,
                            datatype=EVAL_TYPES,
                            max_rows=5,
                        )
                with gr.Accordion("🔄 Running Evaluation Queue", open=False):
                    with gr.Row():
                        running_eval_table = gr.components.Dataframe(
                            value=running_eval_queue_df,
                            headers=EVAL_COLS,
                            datatype=EVAL_TYPES,
                            max_rows=5,
                        )

                with gr.Accordion("⏳ Pending Evaluation Queue", open=False):
                    with gr.Row():
                        pending_eval_table = gr.components.Dataframe(
                            value=pending_eval_queue_df,
                            headers=EVAL_COLS,
                            datatype=EVAL_TYPES,
                            max_rows=5,
                        )

                with gr.Row():
                    refresh_button = gr.Button("Refresh")
                    refresh_button.click(
                        refresh,
                        inputs=[],
                        outputs=[
                            leaderboard_table,
                            finished_eval_table,
                            running_eval_table,
                            pending_eval_table,
                        ],
                    )
                with gr.Accordion("Submit a new model for evaluation"):
                    with gr.Row():
                        with gr.Column():
                            model_name_textbox = gr.Textbox(label="Model name")
                            revision_name_textbox = gr.Textbox(
                                label="revision", placeholder="main"
                            )

                        with gr.Column():
                            is_8bit_toggle = gr.Checkbox(
                                False, label="8 bit eval", visible=not IS_PUBLIC
                            )
                            private = gr.Checkbox(
                                False, label="Private", visible=not IS_PUBLIC
                            )
                            is_delta_weight = gr.Checkbox(False, label="Delta weights")
                            base_model_name_textbox = gr.Textbox(
                                label="base model (for delta)"
                            )

                    submit_button = gr.Button("Submit Eval")
                    submission_result = gr.Markdown()
                    submit_button.click(
                        add_new_eval,
                        [
                            model_name_textbox,
                            base_model_name_textbox,
                            revision_name_textbox,
                            is_8bit_toggle,
                            private,
                            is_delta_weight,
                        ],
                        submission_result,
                    )
        with gr.TabItem(
            "🧑‍⚖️ Human & GPT-4 Evaluations 🤖", elem_id="human-gpt-tab-table", id=1
        ):
            with gr.Row():
                with gr.Column(scale=2):
                    gr.Markdown(HUMAN_GPT_EVAL_TEXT, elem_classes="markdown-text")
                with gr.Column(scale=1):
                    gr.Image(
                        "src/assets/scale-hf-logo.png", elem_id="scale-logo", show_label=False
                    )
            gr.Markdown("## No tie allowed")
            elo_leaderboard_table = gr.components.Dataframe(
                value=elo_leaderboard,
                headers=ELO_COLS,
                datatype=ELO_TYPES,
                max_rows=5,
            )

            gr.Markdown("## Tie allowed*")
            elo_leaderboard_table_with_tie_allowed = gr.components.Dataframe(
                value=elo_leaderboard_with_tie_allowed,
                headers=ELO_COLS,
                datatype=ELO_TYPES,
                max_rows=5,
            )

            gr.Markdown(
                "\* Results when the scores of 4 and 5 were treated as ties.",
                elem_classes="markdown-text",
            )

            gr.Markdown(
                "Let us know in [this discussion](https://huggingface.co/spaces/HuggingFaceH4/open_llm_leaderboard/discussions/65) which models we should add!",
                elem_id="models-to-add-text",
            )

    dummy = gr.Textbox(visible=False)
    demo.load(
        change_tab,
        dummy,
        tabs,
        _js=get_window_url_params,
    )
    if ADD_PLOTS:
        with gr.Box():
            visualization_title = gr.HTML(VISUALIZATION_TITLE)
            with gr.Row():
                with gr.Column():
                    gr.Markdown(f"#### Figure 1: {PLOT_1_TITLE}")
                    plot_1 = gr.Plot(plot_1, show_label=False)
                with gr.Column():
                    gr.Markdown(f"#### Figure 2: {PLOT_2_TITLE}")
                    plot_2 = gr.Plot(plot_2, show_label=False)
            with gr.Row():
                with gr.Column():
                    gr.Markdown(f"#### Figure 3: {PLOT_3_TITLE}")
                    plot_3 = gr.Plot(plot_3, show_label=False)
                with gr.Column():
                    gr.Markdown(f"#### Figure 4: {PLOT_4_TITLE}")
                    plot_4 = gr.Plot(plot_4, show_label=False)

scheduler = BackgroundScheduler()
scheduler.add_job(restart_space, "interval", seconds=3600)
scheduler.start()
demo.queue(concurrency_count=40).launch()
