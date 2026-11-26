"""Eight spark lanes share the two DGX Sparks through the spark-gateway,
which sends each request to a box with a free engine slot (loom#3605). Each
slot is a lane the station claims for and maps back to the spark runner."""
import unittest

from bus import factory, labels

SPARK_SLOTS = ("spark",) + tuple("spark-%d" % n for n in range(2, 9))


class SparkSlotsTest(unittest.TestCase):
    def test_spark_two_to_eight_are_lanes_of_the_spark_runner(self):
        for slot in SPARK_SLOTS:
            self.assertIn(slot, labels.LANES)
            self.assertEqual(labels.base_lane(slot), "spark")

    def test_the_eight_spark_slots_take_work_first(self):
        slots = tuple(x for x in labels.LANES if labels.base_lane(x) == "spark")
        self.assertEqual(sorted(slots), sorted(SPARK_SLOTS))
        self.assertEqual(factory.SPARKS, SPARK_SLOTS)
        self.assertEqual(factory.LANE_ORDER[:len(SPARK_SLOTS)], SPARK_SLOTS)
        self.assertEqual({factory.lane_kind(x) for x in SPARK_SLOTS}, {"own-hardware"})


if __name__ == "__main__":
    unittest.main()
