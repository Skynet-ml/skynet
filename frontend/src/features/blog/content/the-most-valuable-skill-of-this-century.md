I started writing this the day Levent Alpöge announced a proof of the Riemann Hypothesis, worked out with Claude and formalized in Lean. He set the direction, though as he put it, "this time Claude far outpaced me near the finish line" [1]. A day later, the Lean and Mathlib communities certified both the statement and the proof [2].

Watching a mathematician steer while Lean verified the result sharpened the bet I've been making and what I'm building around it.

## When will AGI arrive?

Models still need far more data than people do to learn the same thing, and Dwarkesh Patel notes it isn't clear we've closed that gap [3]. Even so, steady gains in models, architectures, and data keep compounding, and AI will soon speed up AI research itself. Patel puts his 50/50 bet at 2032 for AI that learns on the job like a human [4], and describes the labs' plan to get there: automate AI research first, then let automated researchers solve sample efficiency [3]. I agree with him, and plenty of others are putting a year on it too.

{{figure: timeline | Eight guesses on one line: Elon Musk [5], Dario Amodei [6], [7], Sam Altman [8], Shane Legg [9], [10], Ray Kurzweil [11], Demis Hassabis [12], [13], Dwarkesh Patel [4], and Andrej Karpathy [14].}}

Whichever year turns out right, you don't have to wait for AGI to get far better results today.

## There are seven levers you can pull

A stronger model and higher reasoning effort are only two of the seven ways to improve a result:

{{figure: levers | Six of the seven levers give you a way to improve the result without waiting for a new model.}}

Everyone knows the first two help. Below are the other five, and how far each one can move a result.

{{lever: 5}}

On ARC-AGI-3, GPT-6 Astra ran in a minimal standard harness and in OpenAI's own harness, which keeps reasoning state across requests and compacts long conversations. Same model, same reasoning effort, completely different outcome [15].

{{figure: arc | GPT-6 Astra on ARC-AGI-3 (semi-private set), comparing the standard harness with OpenAI's harness across every reasoning effort ARC Prize tested [15].}}

{{lever: 3}}

On Terminal-Bench 4.1 (66 expert terminal tasks), running a second try already lifts both Claude Fable 5.2 and GPT-6.2 Astra. Combining agents from both models pushes the total higher still, because each model solves tasks the other misses [16].

{{figure: venn | Terminal-Bench 4.1 tasks solved by each model, counting a task as solved when at least three of its five trials pass [16].}}

{{lever: 4}}

Google Research, Google DeepMind, and MIT researchers compared a single agent against four team setups across six benchmarks with the same tools and budget [17]. In an independent setup, agents worked separately without talking to each other and combined their outputs only at the end. In a centralized setup, a manager agent split up the job, handed pieces to worker agents, and checked their answers. In a decentralized setup, agents talked directly to one another as peers with no manager. A hybrid setup combined both, pairing a manager with peer-to-peer talk between workers [17].

The right setup depended on the job. Teams crushed a single agent on parallelizable work like financial research, but hurt performance on step-by-step planning where extra back-and-forth got in the way [17].

{{figure: comms | Mean benchmark score of a single agent versus all four team setups across all six benchmarks [17].}}

{{lever: 6}}

Researchers from UC Berkeley, the University of Chicago, and Bespoke Labs built EnvOpt to optimize the workspace itself without touching the model or harness. It turns messy emails, files, and apps into clean text, action cheatsheets, and searchable indexes [18].

On AutomationBench (200 workplace tasks), EnvOpt built one improved workspace using Claude Haiku 4.5, then dropped four other models straight into that exact same workspace unchanged. Every model scored higher, and EnvOpt even added gains on top of an already-optimized harness [18].

{{figure: env | AutomationBench pass rates before and after EnvOpt reorganized the workspace for Claude Haiku 4.5, with four other models reusing that same workspace unchanged [18].}}

{{lever: 7}}

Extra attempts pay off when your metric can separate the right answer from a plausible wrong one [19]. Researchers at Stanford, UC Berkeley, and NVIDIA showed how much the scoring method itself moves results [19]. They replaced a coarse discrete judge with continuous probability scores, checked repeatedly across clear sub-criteria. Frontier coding and medical agents reached new highs, and the same zero-shot model beat specialized trained reward models [19].

{{figure: verifier | LLM-as-a-Verifier [19]. Picking attempts: on coding and medical benchmarks, each row compares one try, the attempt the verifier picks from the same candidate pool, and a perfect oracle. Scoring method: on RoboRewardBench preference accuracy, only the scoring method changes on the same zero-shot model, and it beats trained robotics reward models.}}

## Problems come in three kinds

1. Problems AI solves in one shot, with no special setup.
2. Problems that need a bespoke setup before AI can solve them, like the ARC-AGI-3 harnesses above [15], [20].
3. Problems AI can't solve yet, where only more training can raise the ceiling.

AI already handles the first kind, and only the labs can work on the third. The middle bucket is the big opportunity for everyone else, and it keeps growing: as models improve and inference gets cheaper, easier tasks move into one-shot work while a larger wave of previously unsolvable problems opens up from above.

{{figure: shift | As models improve and intelligence gets cheaper, one-shot problems grow and previously unsolvable problems move into the bespoke-solution bucket, keeping the middle opportunity large.}}

## Intelligence is getting cheaper fast

Epoch AI finds that since 2023, the cost of a fixed level of AI performance has fallen about 13x per year (47% per quarter) [21]. Reaching 75% on GPQA Diamond cost roughly $0.30 per question with o3 in January 2025, and about $0.0004 with GPT-5.6 Luna 18 months later, a 725-fold drop [21]. Even as the decline slows on older capabilities, cheap intelligence pulls more hard problems into reach every year.

{{figure: price | How many times cheaper each technology became per year, on a log scale. The AI bar is the cost of a fixed level of performance on five benchmarks since 2023. The four comparisons and their periods are Epoch's [21].}}

## My bet is on first-principles thinking

Humans have always worked inside tight constraints: limited time, limited money, limited intelligence. When intelligence gets cheap, the bottleneck shifts from doing the work to deciding what we want and how to measure it.

Most real goals have no finish line: make more money, run faster, be more accurate. Put five people on a problem and you are working with five minds. Put the intelligence of 100 or 1,000 people on it and you would approach it completely differently. Copying old habits wastes that capacity; you have to reason up from the goal with a clear metric. That is why I'm betting on first-principles thinking as the most valuable skill of this century.

## Agent swarms wrestle with the environment

With a clear metric, you can launch swarms of agents in parallel inside a diverse harness to test different ideas. It is the scientific method at scale: form a hypothesis, run experiments, revise, repeat. Running more agents covers more ground and brings back more data, even without a smarter model.

{{figure: loop | The loop I expect: one goal with a metric, many different attempts in parallel, every attempt measured, and the insights fed back into the next round.}}

Most real problems trace back to fixed rules you discover by running experiments against the world. Making models smarter matters less than orchestrating them to explore until they solve the problem or bring back useful insights.

Agent Plasticity, a benchmark from UC Berkeley, Meta Superintelligence Labs, Princeton, and the University of Washington, tests this exact loop on Dwarkesh Patel's question [4]: can frozen models learn on the job? Each round starts fresh, carrying forward only notes, code, tools, and strategies from earlier rounds, then scores the agent on unseen games [22]. Experience alone drove huge gains without changing model weights, yet raw strength and learning efficiency came apart: some models stalled, and the best learner gained over five times more points per dollar than the top final scorer [22].

{{figure: gains | Agent Plasticity results with frozen model weights [22]. Held-out unseen game scores before and after learning from experience (Hard Chess averages checkpoints 16 to 20; Go and Hex show checkpoint 20).}}

{{figure: psat | Agent Plasticity learning efficiency [22]: score points gained per $1,000 of learning cost across Chess, Go, and Hex combined.}}

A lot of software was built under old constraints that no longer hold, so rebuilding from first principles with agent swarms will become common. The only hard limits are the laws of physics; as Elon Musk says, everything else is a recommendation [23].

## Real problems are black-box optimization

Neural-network training gives you gradients that show which way to nudge every weight. Real problems don't. You can't take a derivative through a codebase, a trading system, or a business workflow; you can only try a solution, measure its score, and learn from what comes back.

That is black-box optimization. An agent swarm running against a metric does that search directly: reading errors, rewriting code, and pulling the seven levers until the budget runs out.

## I'm building Skynet to run that loop

You bring a goal and a budget. Skynet runs black-box optimization across the seven levers, working from first principles toward a verifiable solution in that growing middle bucket where the setup decides the outcome [15].

Skynet is model-agnostic and vendor-agnostic, running locally or on the web, because the best model changes every few months and no single provider owns the best harness for every job. We track the latest optimization research and put it in your hands at [skynetml.com](https://skynetml.com).

{{figure: skynet | Skynet at a glance. You bring the goal and a budget, Skynet orchestrates the seven levers from Figure 2, and the loop works toward a solution you can verify.}}

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
15. G. Kamradt, "OpenAI's GPT-6 Astra on ARC-AGI-3," *ARC Prize*, Sept. 3, 2026. [Online]. Available: <https://arcprize.org/blog/astra>
    - 62.7%: standard harness, max effort
    - 98.6%: OpenAI harness, max effort
    - 99.9%: OpenAI harness, high effort, the best run
    - 98.0%: OpenAI harness, low effort
    - 17.5%: standard harness, low effort
    - 35.2%: standard harness, effort none
    - 100%: of the environments can be solved by humans
    - About 3.66x faster: OpenAI harness against standard, on the 167 game and reasoning-effort pairs that both harnesses solved, across the public and semi-private sets
    - 49% fewer: total tokens, in that same comparison
16. Terminal-Bench, "Terminal-Bench 4.1 leaderboard," *tbench.ai*, 2026. Accessed: Nov. 22, 2026. [Online]. Available: <https://www.tbench.ai/leaderboard/terminal-bench/4.1>
    - 66 expert-level terminal tasks, five trials per task in each provider's harness; with two attempts, expected share solved rises from 65.8% to 72.7% for Claude Fable 5.2 and from 62.1% to 66.7% for GPT-6.2 Astra
    - Task overlap (at least three of five trials passing): 45 of 66 solved by Fable, 40 by Astra; 9 by Fable alone, 4 by Astra alone, 36 by both, 49 together
17. Y. Kim et al., "Towards a Science of Scaling Agent Systems," *arXiv:2512.08296*, Dec. 9, 2025, rev. Apr. 8, 2026. [Online]. Available: <https://arxiv.org/abs/2512.08296>
    - 260 configurations: five architectures (single agent, independent, centralized, decentralized, hybrid) across six benchmarks and three model families, with tools, prompts, and compute standardized
    - Finance-Agent: Independent +57%, Centralized +80.8%, Decentralized +74.5%, Hybrid +73.1%
    - BrowseComp-Plus: Independent -35%, Centralized +0.2%, Decentralized +9.2%, Hybrid +6%
    - WorkBench: Independent -11%, Centralized -1.2%, Decentralized +5.6%, Hybrid -1.2%
    - SWE-bench Verified: Independent -14.9%, Centralized -3.1%, Decentralized -5.4%, Hybrid -2.1%
    - Terminal-Bench: Independent +1.7%, Centralized -19.2%, Decentralized -6%, Hybrid -15%
    - PlanCraft: Independent -70.0%, Centralized -50.3%, Decentralized -41.5%, Hybrid -39.1%
    - Error amplification: 17.2x with independent agents versus 4.4x with centralized coordination
18. M. Li, H. Mao, Q. Mang, H. Li, J. E. Gonzalez, and A. Dimakis, "EnvOpt: Automated Optimization for Agent Environments," *arXiv:2611.07372*, Nov. 5, 2026. [Online]. Available: <https://arxiv.org/abs/2611.07372>
    - Stationary environment optimization: automatically reorganizing a shared workspace (plain-text exports, endpoint cheatsheets, file indexes, SQLite catalogs) without changing the model or harness
    - Five benchmarks (workspace, data engineering, and software engineering tasks): 48% average cost reduction for target agents on unseen test sets with equal or higher pass rates
    - AutomationBench (200 test tasks, Table 1): Claude Haiku 4.5 (target) rises from 2.33% to 16.67% pass rate while cost drops 72.73% ($42.36 to $11.55)
    - Transferred unchanged to four other models on AutomationBench: Claude Haiku 5.5 rises from 20.7% to 32.8% (-68.74% cost, $4.83 to $1.51); GPT-5.6 Luna rises from 19.17% to 24.67% (-88.63% cost, $17.42 to $1.98); Claude Opus 4.8 rises from 28.50% to 33.33% (-77.41% cost, $358.13 to $80.90); Claude Opus 5 rises from 33.83% to 35.17% (-68.13% cost, $236.18 to $75.26)
    - Stacked on top of Meta-Harness for Haiku 4.5 (Table 4): pass rate rises from 7.00% to 18.17% (+11.17 percentage points) while cost drops a further 83.4% ($75.33 to $12.47)
19. J. Kwok, S. Li, P. Atreya, Y. Liu, Y. Jiang, C. Finn, M. Pavone, I. Stoica, and A. Mirhoseini, "LLM-as-a-Verifier: A General-Purpose Verification Framework," *arXiv:2607.05391*, July 7, 2026. [Online]. Available: <https://arxiv.org/abs/2607.05391>
    - Continuous verification: computes expected scores over scoring-token logits rather than discrete judge outputs, scaled via score granularity, repeated checks, and criteria decomposition, with no extra training
    - Table 3 (candidate pool selection): Terminal-Bench V2 (GPT-5.5, 5 tries) rises from 83.1% Pass@1 to 86.5% (oracle 92.1%); SWE-bench Verified (Opus 4.5, Gemini 3 Flash, and MiniMax M2.5, 3 tries) rises from 76.1% mean Pass@1 to 78.2% (oracle 84.4%); MedAgentBench (Claude Opus 4.8, 5 tries) rises from 70.2% Pass@1 to 73.3% (oracle 75.0%)
    - Table 4 (RoboRewardBench, 500 trajectory pairs): same zero-shot Qwen 3.6 35B model jumps from 70.8% as a discrete LLM-as-a-Judge to 87.4% as LLM-as-a-Verifier, beating TOPReward (74.7%), Robometer-4B (78.8%), and RoboReward-8B (81.4%)
20. J. Bort, "Nvidia just showed that the harness, not the AI model, is now the real hero," *TechCrunch*, Aug. 21, 2026. [Online]. Available: <https://techcrunch.com/2026/08/21/nvidia-just-showed-that-the-harness-not-the-ai-model-is-now-the-real-hero>
    - 30% to 100%: Claude Opus 5 on ARC-AGI-3's public set (25 environments), with Nvidia's custom harness
    - AVO: Agentic Variation Operators, the name of the harness
    - Supervisor agent: nudges the main agent away from dead ends and from paths it already tried
    - Under 10%: OpenAI's models on ARC-AGI-3, about tripled after two changes to harness settings
    - 2x the cost: what the wrong harness can mean for the same model, in the words of Databricks' CEO
21. L. Emberson and D. Roodman, "The plunging price of thought," *Epoch AI*, Sept. 22, 2026. [Online]. Available: <https://epoch.ai/publications/the-plunging-price-of-thought>
    - 47% per quarter: the fall in the cost of a fixed level of AI performance since 2023, on five benchmarks
    - 13x per year: the same rate, per year
    - About $0.30 to about $0.0004 per question: Epoch's estimate for 75% (adjusted for guessing) on GPQA Diamond, from o3 (January 2025) to GPT-5.6 Luna just under 18 months later. Epoch calls it a 725-fold drop
    - 66% per quarter: the fall when a level of performance is new state of the art, on average over five benchmarks
    - 32% per quarter: the fall two years later, on the same average; three of the five benchmarks show the slowdown
    - Average price decline per year of older technologies: DNA sequencing 1.84x (2001 to 2025), compute 1.51x (1940 to 2001), lithium-ion batteries 1.16x (1991 to 2024), US residential electricity 1.05x (1892 to 1973)
    - Caution: a benchmark score is not the same as useful work, the data cover about three years, and Epoch says the figures should not be read as exact
22. H. Singh et al., "Agent Plasticity: Measuring Self-Improvement Through Experience," *arXiv:2610.08902*, Oct. 6, 2026. [Online]. Available: <https://arxiv.org/abs/2610.08902>. Summary thread: H. Singh, *X*, Nov. 2026. [Online]. Available: <https://x.com/harman26singh/status/2124911857064640826>
    - Frozen-weight self-improvement: each round starts fresh and inherits notes, code, tools, and strategies written after earlier rounds, scored on unseen Chess, Go, Hex, and NetHack games
    - Held-out unseen game gains: Claude Fable 5 rises from 20% to 80% on 5x5 Go (checkpoints 0 to 20) and 37.5% to 73.3% on Hard Chess (checkpoint 0 to checkpoints 16 to 20 average); Claude Opus 5 rises from 25.0% to 66.4% on Hard Chess; GPT-5.6 Sol rises from 40% to 77.5% on 6x6 Hex (checkpoints 0 to 20); Claude Opus 5.5 triples its NetHack mean score from about 2,000 to 6,000 (checkpoints 0 to 2 vs. 36 to 40)
    - Agent plasticity (score points gained per $1,000 of learning cost across Chess, Go, and Hex): GPT-5.6 Sol 298, Claude Opus 5 233, Claude Fable 5 57 (highest final score), Claude Opus 4.8 14, GPT-5.6 Luna 0 (no reliable gain)
23. W. Isaacson, *Elon Musk*. New York, NY, USA: Simon & Schuster, 2023, ch. 46, "Fremont Factory Hell."
    - The algorithm: Musk's rules for questioning requirements, which the book sets out in this chapter
    - The corollary: only the laws of physics are real rules, and every other requirement is a recommendation


Portraits in the timeline figure, cropped, from Wikimedia Commons: [Dario Amodei](https://commons.wikimedia.org/wiki/File:Dario_Amodei_at_TechCrunch_Disrupt_2023_01_(cropped).jpg) by TechCrunch, CC BY 2.0; [Shane Legg](https://commons.wikimedia.org/wiki/File:Shane_Legg.jpg) by Libor.burian, CC BY-SA 4.0; [Ray Kurzweil](https://commons.wikimedia.org/wiki/File:Ray-Kurzweil-at-SXSW_2024_7_(cropped).jpg) by Jay Dixit, CC BY 4.0; [Demis Hassabis](https://commons.wikimedia.org/wiki/File:Demis_Hassabis,_2024_Nobel_Prize_Laureate_in_Chemistry_7_(cropped).jpg) by John Sears, CC BY-SA 4.0; [Andrej Karpathy](https://commons.wikimedia.org/wiki/File:Andrej_Karpathy,_OpenAI.png) by Gladwin Analytics, CC BY 3.0; [Dwarkesh Patel](https://commons.wikimedia.org/wiki/File:Dwarkesh_Patel_2025_(cropped).jpg) by TechCrunch, CC BY 4.0; [Sam Altman](https://commons.wikimedia.org/wiki/File:Sam_Altman_TechCrunch_SF_2019_Day_2_Oct_3_(cropped).jpg) by TechCrunch, CC BY 2.0; [Elon Musk](https://commons.wikimedia.org/wiki/File:Elon_Musk_Royal_Society_crop.jpg) by Debbie Rowe, CC BY-SA 3.0.