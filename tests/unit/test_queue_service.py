class TestQueueService:
    async def test_dequeue_times_out_on_empty_queue(self, queue_service):
        assert await queue_service.dequeue(timeout_seconds=1) is None

    async def test_enqueue_then_dequeue_round_trips(self, queue_service):
        await queue_service.enqueue("doc1")

        assert await queue_service.dequeue(timeout_seconds=1) == "doc1"

    async def test_dequeue_delivers_in_fifo_order(self, queue_service):
        await queue_service.enqueue("doc1")
        await queue_service.enqueue("doc2")

        assert await queue_service.dequeue(timeout_seconds=1) == "doc1"
        assert await queue_service.dequeue(timeout_seconds=1) == "doc2"
