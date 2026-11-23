I started writing this on the day we learned that the Riemann Hypothesis, the most famous open problem in math, might have been solved with AI. Levent Alpöge, a mathematician at Anthropic, announced a proof he worked out with Claude and formalized in Lean. He set the direction himself, combining two ideas and guiding the work, though by his own account "this time Claude far outpaced me near the finish line" [1]. A day later, the Lean and Mathlib communities certified both the statement and the proof: the theorem matches Mathlib's own statement of the Riemann Hypothesis, and Lean's kernel along with several independent checkers accepted it [2].

Seeing a proof like that come together, with a mathematician steering and Lean verifying the result, sharpened the bet I've been making and what I'm building around it.

## When will AGI arrive?

Today's models still need far more data than people do to learn the same thing. Dwarkesh Patel estimates that humans are thousands to millions of times more sample efficient than current models, and notes it isn't clear we've made much progress closing that gap [3]. Even without a breakthrough there, small improvements in models, architectures, and data keep compounding. That gives us a strong chance of reaching artificial general intelligence soon, because AI will speed up the research process itself.

Patel sketches a similar path. In June 2025 he argued that models still don't improve over time the way a person does, putting his 50/50 bet at 2032 for AI that learns on the job as well as a human in any white-collar role. Even in that post, though, he called misaligned superintelligence as early as 2028 a plausible outcome [4]. A year later he described how the labs plan to tackle jobs you can't train for in advance: automate AI research first, then let the automated researchers solve sample efficiency [3]. I agree with him. And he isn't the only one putting a year on it.

{{figure: timeline | Eight guesses on one line: Elon Musk [5], Dario Amodei [6], [7], Sam Altman [8], Shane Legg [9], [10], Ray Kurzweil [11], Demis Hassabis [12], [13], Dwarkesh Patel [4], and Andrej Karpathy [14].}}

Whichever date turns out right, you don't have to wait for AGI to get far better results today.

## There are six levers you can pull

A newer model is only one of six ways to push performance higher, and five other choices are already in your hands. I'll walk through all six using different benchmarks and settings to show how much each lever can move the result:

{{figure: levers | Every lever except the first gives you a way to improve the result without waiting for a new model.}}

{{lever: 1}}

On ARC-AGI-3, holding the harness fixed at ARC Prize's standard setup and taking each model at its best reasoning effort, scores run from 0.5% for Claude Haiku 5.5 and 30.2% for Claude Opus 5 up to 81.2% for Claude Fable 5.2, with GPT-6 Astra at 62.7% [15].

{{figure: arcmodels | Different models on ARC-AGI-3 with the same standard harness, as listed on ARC Prize's leaderboard [15].}}

{{lever: 2, 4}}

Inside ARC-AGI-3's standard harness, a minimal interface where the model decides which notes to carry forward, turning reasoning effort from low to max lifts GPT-6 Astra from 17.5% to 62.7% [16]. Swap the harness for OpenAI's own setup, which changes context management by keeping the model's reasoning state between requests and compacting long conversations, and the exact same model at max effort jumps from 62.7% to 98.6% [16].

{{figure: arc | GPT-6 Astra on ARC-AGI-3 (semi-private set), at every reasoning effort ARC Prize tested [16].}}

{{lever: 3}}

On Terminal-Bench 4.0, which has 66 expert-level terminal tasks across software, science, machine learning, operations, and security, the top frontier models sit close together: when Vals AI ran them in the same minimal agent and averaged three runs, Claude Fable 5.2 solved 65.15% of the tasks and GPT-6.2 Astra solved 63.13% [17].

Running more attempts lifts solve rates further. On the official Terminal-Bench 4.1 leaderboard, each model ran five trials per task inside its own vendor's agent. Giving a model two attempts instead of one raises the expected share of tasks solved from 65.8% to 72.7% for Fable and from 62.1% to 66.7% for Astra [17]. Combining agents from both models pushes the total higher still. Counting a task as solved when at least three of five trials pass, Fable clears 45 of the 66 tasks and Astra clears 40. Fable gets nine that Astra misses, and Astra gets four that Fable misses, bringing the combined total to 49 [17]. Some of that split may come from the agents, since each model ran in its own vendor's setup.

{{figure: venn | Terminal-Bench 4.1 tasks solved by each model, counting a task as solved when at least three of its five trials pass [17].}}

How those agents communicate matters just as much as how many you run. Researchers from Google Research, Google DeepMind, and MIT tested a single agent against four multi-agent coordination structures across 260 configurations on six agentic benchmarks, holding tools, prompts, and compute steady [18]. On decomposable financial analysis (Finance-Agent), every multi-agent setup beat a single agent, with centralized coordination lifting scores by up to 80.8%. On sequential planning (PlanCraft), every multi-agent setup hurt performance, dropping scores by 39% to 70% because coordination overhead broke up step-by-step reasoning [18]. How agents communicate also controlled how errors spread: independent agents amplified mistakes 17.2x, while centralized coordination kept that down to 4.4x [18].

{{figure: comms | Relative score change of multi-agent setups against a single agent across four of those benchmarks, plus error amplification by coordination style [18].}}

{{lever: 5}}

OSWorld tests AI agents on 369 real computer tasks across web and desktop apps, varying how the model sees and acts on the desktop [19]. Giving the exact same model a different interface to that desktop more than doubled its success rate. With raw screenshots alone, GPT-4V solved 5.26% of the tasks; adding the accessibility tree lifted it to 12.17% [19]. GPT-4o climbed from 5.03% on screenshots to 11.36% with the accessibility tree, and Claude 3 Opus went from 2.42% to 6.72% with Set-of-Mark overlays [19]. Which interface works best depends on the model too: Set-of-Mark lifted Claude 3 Opus to its top score, yet dropped GPT-4o to 4.59% [19].

{{figure: env | Three models on OSWorld's 369 desktop tasks, moving from raw screenshots to each model's best structured interface [19].}}

{{lever: 6}}

Extra attempts and agents only pay off if the system can score and verify which attempt worked. A study by researchers at Stanford, Oxford, and Google DeepMind shows how sharp that divide is [20]. On SWE-bench Lite, where unit tests automatically verify each proposed fix, drawing 250 samples from DeepSeek-Coder-V2-Instruct lifted its solve rate from 15.9% on one try to 56%, beating the single-attempt frontier record of 43% at the time [20]. On MATH, where no automatic test confirms an answer, the picture splits wide open. Scaling Llama-3-8B-Instruct from 100 to 10,000 samples raised the share of problems with at least one right answer in the pool from 82.9% to 98.44%, yet majority voting and reward models barely budged, rising at best from 40.50% to 41.41% [20].

{{figure: metric | Repeated sampling with and without an automatic verifier: SWE-bench Lite with unit tests, and MATH with an oracle verifier against majority voting or reward models [20].}}

## A benchmark score measures the harness too

Results like these make benchmarks close to useless as a measure of the model alone. They still tell you something about what a model can do in a domain, and ARC Prize keeps a standard harness so you can compare models from different providers apples to apples [16]. Still, when the same model can score 62.7% or 98.6% depending on what you wrap around it [16], which number tells you what the model can do?

Greg Kamradt, the president of ARC Prize, goes even further. After GPT-6.2 Astra became the first model to hit 100% on ARC-AGI-3 in ARC Prize's verified results on November 17, 2026, he concluded that static task completion is no longer an adequate way to measure frontier models [21].

Astra's 100% came from a harness built for the model [21]. In August, TechCrunch reported that Nvidia researchers pushed Claude Opus 5 from 30% to 100% on the benchmark's public set with a custom harness [22]. Sometimes all a harness adds is a small nudge. Nvidia's setup uses a supervisor agent that steers the main agent away from dead ends and paths it already tried [22]. The right nudge can turn a stuck attempt into a solved coding problem, or perhaps into something like a proof of the Riemann Hypothesis, though we still don't know how much guidance Alpöge gave Claude along the way [1]. What remains open is how many nudges a model still needs before it can handle problems like these on its own.

{{figure: nvidia | Claude Opus 5 on ARC-AGI-3's public set without and with Nvidia's harness, where a supervisor agent nudges the main agent away from dead ends and repeat paths [22].}}

## Problems come in three kinds

1. Problems AI solves in one shot, with no special changes to the environment.
2. Problems that need a bespoke solution before AI can solve them. Here the harness makes a huge difference, as the ARC-AGI-3 results with and without a custom setup show [16], [22].
3. Problems AI can't solve no matter how you tweak the harness or environment. Those hit a real ceiling in the model itself, and only more training can raise it.

AI already handles the first kind, and only the labs training the models can work on the third. The second kind is the big, immediate opportunity for everyone else.

{{figure: problems | The three kinds of problems. Adjusting the levers around the model pays off in the middle group.}}

## As intelligence gets cheaper, the opportunity keeps growing

Those three buckets do not stand still. Epoch AI's data show how fast intelligence is getting cheaper: since 2023, the cost of a fixed level of AI performance has fallen about 47% per quarter, or 13x per year [23]. In one example, Epoch estimates that OpenAI's o3 cost about $0.30 per question in January 2025 to score 75% on GPQA Diamond. Just under 18 months later, GPT-5.6 Luna hit the same score for about $0.0004 per question, a drop Epoch puts at 725-fold [23].

That decline does slow as a given level of performance ages. Averaged across Epoch's five main benchmarks, prices fall fastest when a capability is new, at 66% per quarter, then slow to 32% per quarter two years later, a pattern visible in three of the five benchmarks. Epoch notes a few caveats: benchmark scores aren't the same as useful work, the data span only about three years, and the numbers shouldn't be read as exact [23]. Even at the slower rate, though, a fixed level of performance gets far cheaper every year.

{{figure: price | How many times cheaper each technology became per year, on a log scale. The AI bar is the cost of a fixed level of performance on five benchmarks since 2023. The four comparisons and their periods are Epoch's [23].}}

As models improve and intelligence gets cheaper, both the first and second kinds of problems grow. Tasks that need a bespoke harness today become routine one-shot work tomorrow, expanding the first group. At the same time, harder problems that sit out of reach in the third group, either beyond current models or too expensive to explore across hundreds of attempts, open up once stronger models and cheap inference make search practical. Even as easier tasks graduate out of the middle group, a larger wave of harder problems moves in from above, so the opportunity there keeps growing.

{{figure: shift | As models improve and intelligence gets cheaper, one-shot problems grow and previously unsolvable problems move into the bespoke-solution bucket, keeping the middle opportunity large.}}

## My bet is on first-principles thinking

Humans have always worked inside real constraints: limited time, limited money, limited anything else you can name. Those constraints haven't disappeared, but AI loosens them, and that shift is only speeding up. When intelligence gets cheap, the bottleneck shifts from doing the work to deciding what the work should be: what do we want, and how do we measure it?

Behind most tasks sits a higher-level goal: make more money, run faster, be more accurate. Goals like these have no finish line, because you can always push the result further. Put five people on a problem at a company, and what goes into it is their combined intelligence along with their time and resources. Intelligence is a basic resource right next to time and money, which is why AI changes so much once it can handle a task as well as people do: it makes that resource cheap. Put the intelligence of 100 or 1,000 people on a problem, and you would approach it in a completely different way. When old constraints loosen like that, copying how things used to be done wastes the new capacity; you have to reason up from the underlying goal. That is why I'm betting on first-principles thinking as the most needed skill of this century, and why you need a clear metric to guide it.

## Agent swarms wrestle with the environment

Once you have a metric, you can launch swarms of agents in parallel inside a harness built for diversity, so they explore different solutions. That appeals to me because it's how good solutions come about in the real world: we wrestle with the environment. It's the scientific method. You form a hypothesis, run experiments, revise your assumptions, and so on and so forth.

Agent swarms do the same thing at scale. Like the harness, the number of agents is a lever you can pull without a new model: the more agents you run, the more ground you cover and the more results come back, even if no single agent gets smarter. Covering more ground also gives you intelligence in the military sense of the word: more data. Data, and the connections between data points, are how we build an understanding of the world we're working in.

{{figure: loop | The loop I expect: one goal with a metric, many different attempts in parallel, every attempt measured, and the insights fed back into the next round.}}

In the end, almost everything there is to learn traces back to the laws of the universe. Those laws are fixed, and you discover how they play out by running experiments against your problem. Building a bespoke setup to explore it is what pays off. For the same reason, making models smarter matters less than making them better at orchestration: exploring the space until they either solve the problem or bring back useful insights.

I picture a world where we rebuild things from first principles. A lot of software, and plenty outside it, was built under constraints I don't think apply anymore, so rewriting with agent swarms will probably become much more common. Doing that well takes two things: measuring and verifying a goal, and iterating continuously toward a better solution. The only hard limits are the laws of physics; as Elon Musk likes to say, everything else is a recommendation [24].

## Real problems are black-box optimization

When you train a neural network, you get gradients that tell you which way to nudge every weight. Most real problems give you nothing like that. You can't take a derivative through a production codebase, a trading system, a compiler, or a business workflow. All you can do is try a candidate solution inside the environment, measure its score, and learn from what came back.

In optimization, that is a black box: you control the inputs and observe the score, without an equation connecting the two. Running an agent swarm against a metric is black-box optimization with agents doing the search: reading error logs, inspecting what failed, forming new hypotheses, rewriting code, and pulling the six levers to push the score higher until the budget runs out.

## I'm building Skynet to run that loop

You bring the goal and a budget. Skynet aims to deliver black-box optimization across any part of the six levers: the model, reasoning effort, attempts and agents, the harness, the environment, and the metric. It works from first principles toward a verifiable solution in that expanding middle category of problems, where the setup around the model decides the outcome, as the jump from 62.7% to 98.6% on ARC-AGI-3 shows [16].

We're designing Skynet as a model-agnostic, vendor-agnostic tool that runs locally or on the web, because the best model changes every few months and no single provider has the best harness for every job. Our plan is to stay close to the research, track the optimization algorithms out there, and make them available to anyone who wants to use them. You can find Skynet at [skynetml.com](https://skynetml.com).

{{figure: skynet | Skynet at a glance. You bring the goal and a budget, Skynet orchestrates the six levers from Figure 2, and the loop works toward a solution you can verify.}}

## References

1. K. Yates, "AI may have helped solve the most famous problem in maths," *Substack*, Nov. 20, 2026. [Online]. Available: <https://kityates.substack.com/p/ai-may-have-helped-solve-the-most>. Statement of the claimed proof. [Online]. Available: <https://alpo.ge/remark.pdf>
    - Claimed: a proof of the Riemann Hypothesis, by Levent Alpöge with Claude
    - Lean: a formalization came with the announcement
    - Near the finish line: "this time Claude far outpaced me near the finish line," in his words in the statement
    - Not written for people yet: he presents only the Lean for now, and says he intends to write the proof properly
2. Lean and Mathlib communities, "Lean verification of the Riemann Hypothesis," *Lean community blog*, Nov. 21, 2026. [Online]. Available: <https://leanprover-community.github.io/blog/posts/riemann-hypothesis/>. Verification files: A. Sutherland, "RH Lean proof: verification files," MIT, updated Nov. 22, 2026. [Online]. Available: <https://math.mit.edu/~drew/grh/>
    - Certified: "both the statement and the proof of the Riemann Hypothesis are correct," in the words of the joint statement, which the Lean FRO and the Mathlib Initiative endorse
    - The statement: the proved theorem matches Mathlib's own definition of the Riemann Hypothesis, which experts in Lean and number theory vetted
    - The checkers: Lean's official kernel and several independent third-party checkers accepted the exported proof
    - 17 million: lines in the library, which depends directly only on Mathlib
    - 175,187: modules that compiled with zero errors; the headline theorems depend only on Lean's three standard axioms
    - Still to do: the mathematical community has to explain the new ideas in the proof
3. D. Patel, "The data black hole at the center of AI," *dwarkesh.com*, June 19, 2026. [Online]. Available: <https://www.dwarkesh.com/p/the-sample-efficiency-black-hole>
    - Thousands to millions of times: how much more sample efficient humans are than current models
    - Not clear: whether training sample efficiency made much progress
    - Automate AI research first: the labs' plan for jobs that need out-of-distribution thinking, as he describes it
4. D. Patel, "Why I don't think AGI is right around the corner," *dwarkesh.com*, June 2, 2025. [Online]. Available: <https://www.dwarkesh.com/p/timelines-june-2025>
    - Models do not get better over time the way a person does
    - 2032: his 50/50 bet, as of June 2, 2025, for AI that learns on the job as well as a human, for any white-collar work
    - 2028: misaligned superintelligence (ASI) that early is still a plausible outcome, in his words
5. J. Nelson, "Elon Musk predicts AI will outpace human intelligence as early as this year," *Decrypt*, Jan. 22, 2026. [Online]. Available: <https://decrypt.co/355532/elon-musk-predicts-ai-outpace-human-intelligence-this-year>
    - By the end of 2026: AI smarter than any human, as Elon Musk said on January 22, 2026 at the World Economic Forum in Davos
    - No later than 2027, in the same talk
6. D. Amodei, "The adolescence of technology: Confronting and overcoming the risks of powerful AI," *darioamodei.com*, Jan. 2026. [Online]. Available: <https://darioamodei.com/essay/the-adolescence-of-technology>
    - As little as 1 to 2 years away: powerful AI, as he wrote in January 2026
    - It could also be considerably further out, in the same essay
    - A country of geniuses in a datacenter: how he describes powerful AI
7. J. Kahn, "Anthropic CEO Dario Amodei's 20,000-word essay on how AI 'will test' humanity is a must-read—but more for his remedies than his warnings," *Fortune*, Jan. 27, 2026. [Online]. Available: <https://fortune.com/2026/01/27/anthropic-ceo-dario-amodei-essay-warning-ai-adolescence-test-humanity-risks-remedies/>
    - Monday, January 26, 2026: the day Dario Amodei published the essay. The article of January 27 says Monday
8. T. Sanjay, "By 2028, more of the world's intellectual capacity could reside in data centres than outside: Sam Altman," *Outlook Business*, Feb. 19, 2026. [Online]. Available: <https://www.outlookbusiness.com/news/by-2028-more-of-the-worlds-intellectual-capacity-could-reside-in-data-centres-than-outside-says-sam-altman>
    - By the end of 2028: more of the world's intellectual capacity could be inside data centres than outside, as Sam Altman said on February 19, 2026 at the India AI Impact Summit in New Delhi
    - He added that he could be wrong
9. "Google DeepMind founder returns to campus," *University of Auckland*, Nov. 11, 2026. [Online]. Available: <https://www.auckland.ac.nz/en/news/2026/11/11/shane-legg-google-university-auckland-business-school.html>
    - 2028: a 50 percent chance of human-level machine intelligence by then, which Shane Legg predicted more than 20 years ago
    - He has not changed his mind
10. H. Fry, "The arrival of AGI with Shane Legg (co-founder of DeepMind)," *Google DeepMind: The Podcast*, Dec. 11, 2025. [Online]. Available: <https://zeno.fm/podcast/google-deepmind-the-podcast/episodes/the-arrival-of-agi-with-shane-legg-co-founder-of-deepmind>. Reported in: M. Bastian, "Deepmind co-founder Shane Legg sees 50 percent chance of 'minimal AGI' by 2028," *The Decoder*, Dec. 14, 2025. [Online]. Available: <https://the-decoder.com/deepmind-co-founder-shane-legg-sees-50-percent-chance-of-minimal-agi-by-2028/>
    - December 11, 2025: the release date of the episode
    - 50 percent: his odds of minimal AGI by 2028, as The Decoder reports
11. I. Flatow, "'The Singularity Is Nearer,' says futurist Ray Kurzweil," *Science Friday*, June 14, 2024. [Online]. Available: <https://www.sciencefriday.com/segments/ray-kurzweil-the-singularity-is-nearer-book/>
    - 2029: AGI, and a computer that passes the Turing test, which Ray Kurzweil predicted in 1999
    - He kept that date on June 14, 2024, in this interview
12. J. Nelson, "Google DeepMind CEO says AGI is coming fast: 'We don't have long to prepare'," *Decrypt*, June 4, 2026. [Online]. Available: <https://decrypt.co/370080/google-deepmind-ceo-agi-coming>
    - Maybe 2030, plus or minus a year: AGI, as Demis Hassabis said at a Stanford Graduate School of Business event
13. A. Ghosal, "Google DeepMind CEO warns AI is at 'species-level transition'," *The Stanford Daily*, May 29, 2026. [Online]. Available: <https://stanforddaily.com/2026/05/29/deepmind-ceo-warns-about-ai/>
    - Friday, May 22, 2026: the day of the talk with Stanford President Jonathan Levin at the Graduate School of Business. The article of May 29 says last Friday
14. D. Patel, "Andrej Karpathy — AGI is still a decade away," *Dwarkesh Podcast*, Oct. 17, 2025. [Online]. Available: <https://www.dwarkesh.com/p/andrej-karpathy>
    - A decade: how far away AGI feels to Andrej Karpathy, who calls this the decade of agents
    - About 2035: a decade from October 17, 2025, the year the figure uses
15. ARC Prize, "ARC-AGI-3 leaderboard," *ARC Prize*, updated Nov. 19, 2026. Accessed: Nov. 22, 2026. [Online]. Available: <https://arcprize.org/leaderboard>
    - Standard harness, semi-private set, each model at its best reasoning effort
    - 81.2%: Claude Fable 5.2 (xhigh)
    - 70.3%: GPT-6.2 Astra (max)
    - 65.1%: Gemini 4 Carbon (low)
    - 62.7%: GPT-6 Astra (max)
    - 30.2%: Claude Opus 5 (high)
    - 24.3%: Claude Sonnet 5.5 (xhigh)
    - 1.8%: Grok 4.7 (xhigh)
    - 0.5%: Claude Haiku 5.5 (max)
16. G. Kamradt, "OpenAI's GPT-6 Astra on ARC-AGI-3," *ARC Prize*, Sept. 3, 2026. [Online]. Available: <https://arcprize.org/blog/astra>
    - 62.7%: standard harness, max effort
    - 98.6%: OpenAI harness, max effort
    - 99.9%: OpenAI harness, high effort, the best run
    - 98.0%: OpenAI harness, low effort
    - 17.5%: standard harness, low effort
    - 35.2%: standard harness, effort none
    - 100%: of the environments can be solved by humans
    - About 3.66x faster: OpenAI harness against standard, on the 167 game and reasoning-effort pairs that both harnesses solved, across the public and semi-private sets
    - 49% fewer: total tokens, in that same comparison
17. Vals AI, "Terminal-Bench 4.0," *Vals AI Benchmarks*, and Terminal-Bench, "Terminal-Bench 4.1 leaderboard," *tbench.ai*, 2026. Accessed: Nov. 22, 2026. [Online]. Available: <https://www.vals.ai/benchmarks/terminal-bench-4> and <https://www.tbench.ai/leaderboard/terminal-bench/4.1>
    - Terminal-Bench 4.0 (Vals AI): 66 expert-level terminal tasks, same minimal agent, averaged over three runs: Claude Fable 5.2 65.15%, GPT-6.2 Astra 63.13%
    - Terminal-Bench 4.1 (official leaderboard): five trials per task in each vendor's agent; with two attempts, expected share solved rises from 65.8% to 72.7% for Fable and from 62.1% to 66.7% for Astra
    - Task overlap (at least three of five trials passing): 45 of 66 solved by Fable, 40 by Astra; 9 by Fable alone, 4 by Astra alone, 36 by both, 49 together
18. Y. Kim et al., "Towards a Science of Scaling Agent Systems," *arXiv:2512.08296*, Dec. 9, 2025, rev. Apr. 8, 2026. [Online]. Available: <https://arxiv.org/abs/2512.08296>
    - 260 configurations: five architectures (single agent, independent, centralized, decentralized, hybrid) across six benchmarks and three model families, with tools, prompts, and compute standardized
    - +57% to +80.8%: multi-agent gains over a single agent on decomposable financial reasoning (Finance-Agent)
    - -39.1% to -70.0%: multi-agent drops on sequential planning (PlanCraft), with -39.1% for hybrid, -50.3% for centralized, and -70.0% for independent
    - -11% to +6% on WorkBench; -15% to -2% on SWE-bench Verified
    - Error amplification: 17.2x with independent agents versus 4.4x with centralized coordination
19. T. Xie et al., "OSWorld: Benchmarking Multimodal Agents for Open-Ended Tasks in Real Computer Environments," *NeurIPS*, 2024. [Online]. Available: <https://arxiv.org/abs/2404.07972>
    - 369 real computer tasks across web and desktop applications, with execution-based evaluation scripts (human performance: 72.36%)
    - GPT-4V: 5.26% with screenshots only, 11.77% with Set-of-Mark, 12.17% with screenshots and accessibility tree
    - GPT-4o: 5.03% with screenshots only, 4.59% with Set-of-Mark, 11.21% with screenshots and accessibility tree, 11.36% with accessibility tree only
    - Claude 3 Opus: 2.42% with screenshots only, 4.41% with screenshots and accessibility tree, 6.72% with Set-of-Mark
20. B. Brown et al., "Large Language Monkeys: Scaling Inference Compute with Repeated Sampling," *arXiv:2407.21787*, July 31, 2024, rev. Dec. 30, 2024. [Online]. Available: <https://arxiv.org/abs/2407.21787>
    - SWE-bench Lite (unit tests verify each patch): DeepSeek-Coder-V2-Instruct rises from 15.9% at 1 sample to 56% at 250 samples, beating the 43% single-sample state of the art
    - MATH (Llama-3-8B-Instruct, 100 to 10,000 samples): coverage with a perfect verifier rises from 82.9% to 98.44%
    - Without an automatic verifier on MATH: across majority voting and reward models, the largest gain is only from 40.50% to 41.41% over the same 100 to 10,000 sample range
21. G. Kamradt, "This is the first 100% we've seen on v3," *X*, Nov. 17, 2026. [Online]. Available: <https://x.com/GregKamradt/status/2122759751159447888>. Quoted post: ARC Prize, "GPT-6.2 Astra from OpenAI on ARC-AGI (Verified)," *X*, Nov. 17, 2026. [Online]. Available: <https://x.com/arcprize/status/2122751233442156678>
    - 100%: GPT-6.2 Astra on ARC-AGI-3 with the OpenAI harness, about $4,100, the first 100% score ARC Prize reports on the benchmark
    - 70.3%: the same model in the standard harness, about $24,200
    - Not adequate anymore: static task completion as a measure of frontier models, in his view
22. J. Bort, "Nvidia just showed that the harness, not the AI model, is now the real hero," *TechCrunch*, Aug. 21, 2026. [Online]. Available: <https://techcrunch.com/2026/08/21/nvidia-just-showed-that-the-harness-not-the-ai-model-is-now-the-real-hero>
    - 30% to 100%: Claude Opus 5 on ARC-AGI-3's public set (25 environments), with Nvidia's custom harness
    - AVO: Agentic Variation Operators, the name of the harness
    - Supervisor agent: nudges the main agent away from dead ends and from paths it already tried
    - Under 10%: OpenAI's models on ARC-AGI-3, about tripled after two changes to harness settings
    - 2x the cost: what the wrong harness can mean for the same model, in the words of Databricks' CEO
23. L. Emberson and D. Roodman, "The plunging price of thought," *Epoch AI*, Sept. 22, 2026. [Online]. Available: <https://epoch.ai/publications/the-plunging-price-of-thought>
    - 47% per quarter: the fall in the cost of a fixed level of AI performance since 2023, on five benchmarks
    - 13x per year: the same rate, per year
    - About $0.30 to about $0.0004 per question: Epoch's estimate for 75% (adjusted for guessing) on GPQA Diamond, from o3 (January 2025) to GPT-5.6 Luna just under 18 months later. Epoch calls it a 725-fold drop
    - 66% per quarter: the fall when a level of performance is new state of the art, on average over five benchmarks
    - 32% per quarter: the fall two years later, on the same average; three of the five benchmarks show the slowdown
    - Average price decline per year of older technologies: DNA sequencing 1.84x (2001 to 2025), compute 1.51x (1940 to 2001), lithium-ion batteries 1.16x (1991 to 2024), US residential electricity 1.05x (1892 to 1973)
    - Caution: a benchmark score is not the same as useful work, the data cover about three years, and Epoch says the figures should not be read as exact
24. W. Isaacson, *Elon Musk*. New York, NY, USA: Simon & Schuster, 2023, ch. 46, "Fremont Factory Hell."
    - The algorithm: Musk's rules for questioning requirements, which the book sets out in this chapter
    - The corollary: only the laws of physics are real rules, and every other requirement is a recommendation


Portraits in the timeline figure, cropped, from Wikimedia Commons: [Dario Amodei](https://commons.wikimedia.org/wiki/File:Dario_Amodei_at_TechCrunch_Disrupt_2023_01_(cropped).jpg) by TechCrunch, CC BY 2.0; [Shane Legg](https://commons.wikimedia.org/wiki/File:Shane_Legg.jpg) by Libor.burian, CC BY-SA 4.0; [Ray Kurzweil](https://commons.wikimedia.org/wiki/File:Ray-Kurzweil-at-SXSW_2024_7_(cropped).jpg) by Jay Dixit, CC BY 4.0; [Demis Hassabis](https://commons.wikimedia.org/wiki/File:Demis_Hassabis,_2024_Nobel_Prize_Laureate_in_Chemistry_7_(cropped).jpg) by John Sears, CC BY-SA 4.0; [Andrej Karpathy](https://commons.wikimedia.org/wiki/File:Andrej_Karpathy,_OpenAI.png) by Gladwin Analytics, CC BY 3.0; [Dwarkesh Patel](https://commons.wikimedia.org/wiki/File:Dwarkesh_Patel_2025_(cropped).jpg) by TechCrunch, CC BY 4.0; [Sam Altman](https://commons.wikimedia.org/wiki/File:Sam_Altman_TechCrunch_SF_2019_Day_2_Oct_3_(cropped).jpg) by TechCrunch, CC BY 2.0; [Elon Musk](https://commons.wikimedia.org/wiki/File:Elon_Musk_Royal_Society_crop.jpg) by Debbie Rowe, CC BY-SA 3.0.