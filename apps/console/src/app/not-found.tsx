import Link from 'next/link';

export default function NotFound() {
  return (
    <div className="empty">
      <h2>That approval isn’t here</h2>
      <p>
        It may have been decided and cleared, or the link may be stale.{' '}
        <Link href="/">Open the queue</Link> to see what is waiting.
      </p>
    </div>
  );
}
