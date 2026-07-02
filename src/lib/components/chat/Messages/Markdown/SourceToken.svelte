<script lang="ts">
	import { LinkPreview } from 'bits-ui';
	import { decodeString } from '$lib/utils';
	import Source from './Source.svelte';

	export let id;
	export let token;
	export let sourceIds = [];
	export let onClick: Function = () => {};

	let containerElement;
	let openPreview = false;

	// Helper function to return only the domain from a URL
	function getDomain(url: string): string {
		const domain = url.replace('http://', '').replace('https://', '').split(/[/?#]/)[0];

		if (domain.startsWith('www.')) {
			return domain.slice(4);
		}
		return domain;
	}

	// Helper function to check if text is a URL and return the domain
	function formattedTitle(title: string): string {
		if (title.startsWith('http')) {
			return getDomain(title);
		}

		return title;
	}

	const getDisplayTitle = (title: string) => {
		if (!title) return 'N/A';
		if (title.length > 30) {
			return title.slice(0, 15) + '...' + title.slice(-10);
		}
		return title;
	};
</script>

{#if sourceIds}
	{#if (token?.ids ?? []).length == 1}
		{@const id = token.ids[0]}
		{@const identifier = token.citationIdentifiers ? token.citationIdentifiers[0] : id - 1}
		<Source id={identifier} title={sourceIds[id - 1]} {onClick} />
	{:else}
		<LinkPreview.Root openDelay={0} bind:open={openPreview}>
			<LinkPreview.Trigger>
				<button
					aria-label={`${getDisplayTitle(formattedTitle(decodeString(sourceIds[token.ids[0] - 1])))} +${(token?.ids ?? []).length - 1} more sources`}
					class="assistant-inline-source-group w-fit"
					on:click={() => {
						openPreview = !openPreview;
					}}
				>
					<span class="line-clamp-1">
						{getDisplayTitle(formattedTitle(decodeString(sourceIds[token.ids[0] - 1])))}
						<span class="text-white/50">+{(token?.ids ?? []).length - 1}</span>
					</span>
				</button>
			</LinkPreview.Trigger>
			<LinkPreview.Portal>
				<LinkPreview.Content class="z-[999]" align="start" strategy="fixed" sideOffset={6}>
					<div
						class="rounded-2xl border border-white/12 bg-black/95 p-1.5 shadow-[0_18px_48px_rgba(0,0,0,0.45)] cursor-pointer"
					>
						{#each token.citationIdentifiers ?? token.ids as identifier}
							{@const id =
								typeof identifier === 'string' ? parseInt(identifier.split('#')[0]) : identifier}
							<div class="">
								<Source id={identifier} title={sourceIds[id - 1]} {onClick} />
							</div>
						{/each}
					</div>
				</LinkPreview.Content>
			</LinkPreview.Portal>
		</LinkPreview.Root>
	{/if}
{:else}
	<span>{token.raw}</span>
{/if}
