<script lang="ts">
	import { getContext, onDestroy } from 'svelte';

	import { getChatContext } from '$lib/apis/chats';

	import Tooltip from '../common/Tooltip.svelte';
	import ChartBar from '../icons/ChartBar.svelte';

	const i18n = getContext('i18n');

	export let chatId: string | null = null;

	let context: {
		messages: number;
		images: number;
		tokens: number;
		tokens_last_request: number | null;
		budget: number;
		over_budget: boolean;
		compacted_messages: number;
	} | null = null;

	const formatTokens = (value: number) => {
		if (value >= 1_000_000) {
			return `${(value / 1_000_000).toFixed(1)}M`;
		}
		if (value >= 1000) {
			return `${Math.round(value / 1000)}k`;
		}
		return `${value}`;
	};

	const load = async (id: string) => {
		try {
			context = await getChatContext(localStorage.token, id);
		} catch (error) {
			context = null;
		}
	};

	$: if (chatId) {
		load(chatId);
	} else {
		context = null;
	}

	// The chat grows with every message - refresh the numbers from time to time.
	const interval = setInterval(() => {
		if (chatId && document.visibilityState === 'visible') {
			load(chatId);
		}
	}, 30_000);

	onDestroy(() => clearInterval(interval));

	$: percent =
		context && context.budget > 0
			? Math.min(100, Math.round((100 * context.tokens) / context.budget))
			: 0;

	$: color =
		context && context.over_budget
			? 'text-red-500 dark:text-red-400'
			: percent > 80
				? 'text-yellow-600 dark:text-yellow-400'
				: 'text-gray-500 dark:text-gray-400';

	// Only show the indicator when the chat is big enough to matter.
	$: visible = !!context && (context.tokens > 4000 || context.images > 0 || context.messages > 40);

	$: tooltip = context
		? [
				$i18n.t('Context: {{used}} of {{budget}} tokens', {
					used: formatTokens(context.tokens),
					budget: formatTokens(context.budget)
				}),
				context.tokens_last_request
					? $i18n.t('Last request: {{tokens}} tokens', {
							tokens: formatTokens(context.tokens_last_request)
						})
					: '',
				$i18n.t('Messages: {{count}}', { count: context.messages }),
				context.images ? $i18n.t('Images: {{count}}', { count: context.images }) : '',
				context.compacted_messages
					? $i18n.t('Older messages are condensed: {{count}}', {
							count: context.compacted_messages
						})
					: ''
			]
				.filter(Boolean)
				.join('<br/>')
		: '';
</script>

{#if visible && context}
	<Tooltip content={tooltip}>
		<div class="flex items-center gap-1 px-1.5 {color}">
			<div class="m-auto self-center">
				<ChartBar className="size-4" strokeWidth="1.5" />
			</div>
			<span class="text-xs font-medium">{formatTokens(context.tokens)}</span>
		</div>
	</Tooltip>
{/if}
